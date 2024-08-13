from __future__ import annotations

import json
import logging
import re
import secrets
from collections.abc import Iterable
from dataclasses import asdict, dataclass, field, is_dataclass
from enum import Enum
from typing import (
    TYPE_CHECKING,
    Any,
    Sequence,
    Union,
    cast,
)

from cassandra.cluster import ConsistencyLevel, PreparedStatement, Session
from cassio.config import check_resolve_keyspace, check_resolve_session

from ._mmr_helper import MmrHelper
from .concurrency import ConcurrentQueries
from .content import Kind
from .links import Link

if TYPE_CHECKING:
    from .embedding_model import EmbeddingModel

logger = logging.getLogger(__name__)

CONTENT_ID = "content_id"

CONTENT_COLUMNS = "content_id, kind, text_content, links_blob, metadata_blob"

SELECT_CQL_TEMPLATE = (
    "SELECT {columns} FROM {table_name}{where_clause}{order_clause}{limit_clause};"
)


@dataclass
class Node:
    """Node in the GraphStore."""

    text: str
    """Text contained by the node."""
    id: str | None = None
    """Unique ID for the node. Will be generated by the GraphStore if not set."""
    metadata: dict[str, Any] = field(default_factory=dict)
    """Metadata for the node."""
    links: set[Link] = field(default_factory=set)
    """Links for the node."""


class SetupMode(Enum):
    """Mode used to create the Cassandra table."""

    SYNC = 1
    ASYNC = 2
    OFF = 3


class MetadataIndexingMode(Enum):
    """Mode used to index metadata."""

    DEFAULT_TO_UNSEARCHABLE = 1
    DEFAULT_TO_SEARCHABLE = 2


MetadataIndexingType = Union[tuple[str, Iterable[str]], str]
MetadataIndexingPolicy = tuple[MetadataIndexingMode, set[str]]


def _is_metadata_field_indexed(field_name: str, policy: MetadataIndexingPolicy) -> bool:
    p_mode, p_fields = policy
    if p_mode == MetadataIndexingMode.DEFAULT_TO_UNSEARCHABLE:
        return field_name in p_fields
    if p_mode == MetadataIndexingMode.DEFAULT_TO_SEARCHABLE:
        return field_name not in p_fields
    raise ValueError(f"Unexpected metadata indexing mode {p_mode}")


def _serialize_metadata(md: dict[str, Any]) -> str:
    if isinstance(md.get("links"), set):
        md = md.copy()
        md["links"] = list(md["links"])
    return json.dumps(md)


def _serialize_links(links: set[Link]) -> str:
    class SetAndLinkEncoder(json.JSONEncoder):
        def default(self, obj: Any) -> Any:
            if not isinstance(obj, type) and is_dataclass(obj):
                return asdict(obj)

            if isinstance(obj, Iterable):
                return list(obj)

            # Let the base class default method raise the TypeError
            return super().default(obj)

    return json.dumps(list(links), cls=SetAndLinkEncoder)


def _deserialize_metadata(json_blob: str | None) -> dict[str, Any]:
    # We don't need to convert the links list back to a set -- it will be
    # converted when accessed, if needed.
    return cast(dict[str, Any], json.loads(json_blob or ""))


def _deserialize_links(json_blob: str | None) -> set[Link]:
    return {
        Link(kind=link["kind"], direction=link["direction"], tag=link["tag"])
        for link in cast(list[dict[str, Any]], json.loads(json_blob or ""))
    }


def _row_to_node(row: Any) -> Node:
    metadata = _deserialize_metadata(row.metadata_blob)
    links = _deserialize_links(row.links_blob)
    return Node(
        id=row.content_id,
        text=row.text_content,
        metadata=metadata,
        links=links,
    )


_CQL_IDENTIFIER_PATTERN = re.compile(r"[a-zA-Z][a-zA-Z0-9_]*")


@dataclass
class _Edge:
    target_content_id: str
    target_text_embedding: list[float]
    target_link_to_tags: set[tuple[str, str]]


class GraphStore:
    """A hybrid vector-and-graph store backed by Cassandra.

    Document chunks support vector-similarity search as well as edges linking
    documents based on structural and semantic properties.

    Args:
        embedding: The embeddings to use for the document content.
        setup_mode: Mode used to create the Cassandra table (SYNC,
            ASYNC or OFF).
    """

    def __init__(
        self,
        embedding: EmbeddingModel,
        *,
        node_table: str = "graph_nodes",
        targets_table: str = "",
        session: Session | None = None,
        keyspace: str | None = None,
        setup_mode: SetupMode = SetupMode.SYNC,
        metadata_indexing: MetadataIndexingType = "all",
        insert_timeout: float = 30.0,
    ):
        self._insert_timeout = insert_timeout
        if targets_table:
            logger.warning(
                "The 'targets_table' parameter is deprecated "
                "and will be removed in future versions."
            )

        session = check_resolve_session(session)
        keyspace = check_resolve_keyspace(keyspace)

        if not _CQL_IDENTIFIER_PATTERN.fullmatch(keyspace):
            raise ValueError(f"Invalid keyspace: {keyspace}")

        if not _CQL_IDENTIFIER_PATTERN.fullmatch(node_table):
            raise ValueError(f"Invalid node table name: {node_table}")

        self._embedding = embedding
        self._node_table = node_table
        self._session = session
        self._keyspace = keyspace
        self._prepared_query_cache: dict[str, PreparedStatement] = {}

        self._metadata_indexing_policy = self._normalize_metadata_indexing_policy(
            metadata_indexing=metadata_indexing,
        )

        if setup_mode == SetupMode.SYNC:
            self._apply_schema()
        elif setup_mode != SetupMode.OFF:
            raise ValueError(
                f"Invalid setup mode {setup_mode.name}. "
                "Only SYNC and OFF are supported at the moment"
            )

        # TODO: Parent ID / source ID / etc.
        self._insert_passage = session.prepare(
            f"""
            INSERT INTO {keyspace}.{node_table} (
                content_id, kind, text_content, text_embedding, link_to_tags,
                link_from_tags, links_blob, metadata_blob, metadata_s
            ) VALUES (?, '{Kind.passage}', ?, ?, ?, ?, ?, ?, ?)
            """  # noqa: S608
        )

        self._query_by_id = session.prepare(
            f"""
            SELECT {CONTENT_COLUMNS}
            FROM {keyspace}.{node_table}
            WHERE content_id = ?
            """  # noqa: S608
        )

        self._query_ids_and_link_to_tags_by_id = session.prepare(
            f"""
            SELECT content_id, link_to_tags
            FROM {keyspace}.{node_table}
            WHERE content_id = ?
            """  # noqa: S608
        )

    def table_name(self) -> str:
        """Returns the fully qualified table name."""
        return f"{self._keyspace}.{self._node_table}"

    def _apply_schema(self) -> None:
        """Apply the schema to the database."""
        embedding_dim = len(self._embedding.embed_query("Test Query"))
        self._session.execute(f"""
            CREATE TABLE IF NOT EXISTS {self.table_name()} (
                content_id TEXT,
                kind TEXT,
                text_content TEXT,
                text_embedding VECTOR<FLOAT, {embedding_dim}>,

                link_to_tags SET<TUPLE<TEXT, TEXT>>,
                link_from_tags SET<TUPLE<TEXT, TEXT>>,
                links_blob TEXT,
                metadata_blob TEXT,
                metadata_s MAP<TEXT,TEXT>,

                PRIMARY KEY (content_id)
            )
        """)

        # Index on text_embedding (for similarity search)
        self._session.execute(f"""
            CREATE CUSTOM INDEX IF NOT EXISTS {self._node_table}_text_embedding_index
            ON {self.table_name()}(text_embedding)
            USING 'StorageAttachedIndex';
        """)

        self._session.execute(f"""
            CREATE CUSTOM INDEX IF NOT EXISTS {self._node_table}_link_from_tags
            ON {self.table_name()}(link_from_tags)
            USING 'StorageAttachedIndex';
        """)

        self._session.execute(f"""
            CREATE CUSTOM INDEX IF NOT EXISTS {self._node_table}_metadata_s_index
            ON {self.table_name()}(ENTRIES(metadata_s))
            USING 'StorageAttachedIndex';
        """)

    def _concurrent_queries(self) -> ConcurrentQueries:
        return ConcurrentQueries(self._session)

    # TODO: Async (aadd_nodes)
    def add_nodes(
        self,
        nodes: Iterable[Node],
    ) -> Iterable[str]:
        """Add nodes to the graph store."""
        node_ids: list[str] = []
        texts: list[str] = []
        metadatas: list[dict[str, Any]] = []
        nodes_links: list[set[Link]] = []
        for node in nodes:
            if not node.id:
                node_ids.append(secrets.token_hex(8))
            else:
                node_ids.append(node.id)
            texts.append(node.text)
            metadatas.append(node.metadata)
            nodes_links.append(node.links)

        text_embeddings = self._embedding.embed_texts(texts)

        with self._concurrent_queries() as cq:
            tuples = zip(node_ids, texts, text_embeddings, metadatas, nodes_links)
            for node_id, text, text_embedding, metadata, links in tuples:
                link_to_tags = set()  # link to these tags
                link_from_tags = set()  # link from these tags

                for tag in links:
                    if tag.direction in {"in", "bidir"}:
                        # An incoming link should be linked *from* nodes with the given
                        # tag.
                        link_from_tags.add((tag.kind, tag.tag))
                    if tag.direction in {"out", "bidir"}:
                        link_to_tags.add((tag.kind, tag.tag))

                metadata_s = {
                    k: self._coerce_string(v)
                    for k, v in metadata.items()
                    if _is_metadata_field_indexed(k, self._metadata_indexing_policy)
                }

                metadata_blob = _serialize_metadata(metadata)
                links_blob = _serialize_links(links)
                cq.execute(
                    self._insert_passage,
                    parameters=(
                        node_id,
                        text,
                        text_embedding,
                        link_to_tags,
                        link_from_tags,
                        links_blob,
                        metadata_blob,
                        metadata_s,
                    ),
                    timeout=self._insert_timeout,
                )

        return node_ids

    def _nodes_with_ids(
        self,
        ids: Iterable[str],
    ) -> list[Node]:
        results: dict[str, Node | None] = {}
        with self._concurrent_queries() as cq:

            def node_callback(rows: Iterable[Any]) -> None:
                # Should always be exactly one row here. We don't need to check
                #   1. The query is for a `ID == ?` query on the primary key.
                #   2. If it doesn't exist, the `get_result` method below will
                #      raise an exception indicating the ID doesn't exist.
                for row in rows:
                    results[row.content_id] = _row_to_node(row)

            for node_id in ids:
                if node_id not in results:
                    # Mark this node ID as being fetched.
                    results[node_id] = None
                    cq.execute(
                        self._query_by_id, parameters=(node_id,), callback=node_callback
                    )

        def get_result(node_id: str) -> Node:
            if (result := results[node_id]) is None:
                raise ValueError(f"No node with ID '{node_id}'")
            return result

        return [get_result(node_id) for node_id in ids]

    def mmr_traversal_search(
        self,
        query: str,
        *,
        initial_roots: Sequence[str] = (),
        k: int = 4,
        depth: int = 2,
        fetch_k: int = 100,
        adjacent_k: int = 10,
        lambda_mult: float = 0.5,
        score_threshold: float = float("-inf"),
        metadata_filter: dict[str, Any] = {},  # noqa: B006
    ) -> Iterable[Node]:
        """Retrieve documents from this graph store using MMR-traversal.

        This strategy first retrieves the top `fetch_k` results by similarity to
        the question. It then selects the top `k` results based on
        maximum-marginal relevance using the given `lambda_mult`.

        At each step, it considers the (remaining) documents from `fetch_k` as
        well as any documents connected by edges to a selected document
        retrieved based on similarity (a "root").

        Args:
            query: The query string to search for.
            initial_roots: Optional list of document IDs to use for initializing search.
                The top `adjacent_k` nodes adjacent to each initial root will be
                included in the set of initial candidates. To fetch only in the
                neighborhood of these nodes, set `ftech_k = 0`.
            k: Number of Documents to return. Defaults to 4.
            fetch_k: Number of initial Documents to fetch via similarity.
                Will be added to the nodes adjacent to `initial_roots`.
                Defaults to 100.
            adjacent_k: Number of adjacent Documents to fetch.
                Defaults to 10.
            depth: Maximum depth of a node (number of edges) from a node
                retrieved via similarity. Defaults to 2.
            lambda_mult: Number between 0 and 1 that determines the degree
                of diversity among the results with 0 corresponding to maximum
                diversity and 1 to minimum diversity. Defaults to 0.5.
            score_threshold: Only documents with a score greater than or equal
                this threshold will be chosen. Defaults to -infinity.
            metadata_filter: Optional metadata to filter the results.
        """
        query_embedding = self._embedding.embed_query(query)
        helper = MmrHelper(
            k=k,
            query_embedding=query_embedding,
            lambda_mult=lambda_mult,
            score_threshold=score_threshold,
        )

        # For each unselected node, stores the outgoing tags.
        outgoing_tags: dict[str, set[tuple[str, str]]] = {}

        # Fetch the initial candidates and add them to the helper and
        # outgoing_tags.
        columns = "content_id, text_embedding, link_to_tags"
        adjacent_query = self._get_search_cql(
            has_limit=True,
            columns=columns,
            metadata_keys=list(metadata_filter.keys()),
            has_embedding=True,
            has_link_from_tags=True,
        )

        visited_tags: set[tuple[str, str]] = set()

        def fetch_neighborhood(neighborhood: Sequence[str]) -> None:
            # Put the neighborhood into the outgoing tags, to avoid adding it
            # to the candidate set in the future.
            outgoing_tags.update({content_id: set() for content_id in neighborhood})

            # Initialize the visited_tags with the set of outgoing from the
            # neighborhood. This prevents re-visiting them.
            visited_tags = self._get_outgoing_tags(neighborhood)

            # Call `self._get_adjacent` to fetch the candidates.
            adjacents = self._get_adjacent(
                visited_tags,
                adjacent_query=adjacent_query,
                query_embedding=query_embedding,
                k_per_tag=adjacent_k,
                metadata_filter=metadata_filter,
            )

            new_candidates = {}
            for adjacent in adjacents:
                if adjacent.target_content_id not in outgoing_tags:
                    outgoing_tags[adjacent.target_content_id] = (
                        adjacent.target_link_to_tags
                    )

                    new_candidates[adjacent.target_content_id] = (
                        adjacent.target_text_embedding
                    )
            helper.add_candidates(new_candidates)

        def fetch_initial_candidates() -> None:
            initial_candidates_query = self._get_search_cql(
                has_limit=True,
                columns=columns,
                metadata_keys=list(metadata_filter.keys()),
                has_embedding=True,
            )

            params = self._get_search_params(
                limit=fetch_k,
                metadata=metadata_filter,
                embedding=query_embedding,
            )

            fetched = self._session.execute(
                query=initial_candidates_query, parameters=params
            )
            candidates = {}
            for row in fetched:
                if row.content_id not in outgoing_tags:
                    candidates[row.content_id] = row.text_embedding
                    outgoing_tags[row.content_id] = set(row.link_to_tags or [])
            helper.add_candidates(candidates)

        if initial_roots:
            fetch_neighborhood(initial_roots)
        if fetch_k > 0:
            fetch_initial_candidates()

        # Tracks the depth of each candidate.
        depths = {candidate_id: 0 for candidate_id in helper.candidate_ids()}

        # Select the best item, K times.
        for _ in range(k):
            selected_id = helper.pop_best()

            if selected_id is None:
                break

            next_depth = depths[selected_id] + 1
            if next_depth < depth:
                # If the next nodes would not exceed the depth limit, find the
                # adjacent nodes.
                #
                # TODO: For a big performance win, we should track which tags we've
                # already incorporated. We don't need to issue adjacent queries for
                # those.

                # Find the tags linked to from the selected ID.
                link_to_tags = outgoing_tags.pop(selected_id)

                # Don't re-visit already visited tags.
                link_to_tags.difference_update(visited_tags)

                # Find the nodes with incoming links from those tags.
                adjacents = self._get_adjacent(
                    link_to_tags,
                    adjacent_query=adjacent_query,
                    query_embedding=query_embedding,
                    k_per_tag=adjacent_k,
                    metadata_filter=metadata_filter,
                )

                # Record the link_to_tags as visited.
                visited_tags.update(link_to_tags)

                new_candidates = {}
                for adjacent in adjacents:
                    if adjacent.target_content_id not in outgoing_tags:
                        outgoing_tags[adjacent.target_content_id] = (
                            adjacent.target_link_to_tags
                        )
                        new_candidates[adjacent.target_content_id] = (
                            adjacent.target_text_embedding
                        )
                        if next_depth < depths.get(
                            adjacent.target_content_id, depth + 1
                        ):
                            # If this is a new shortest depth, or there was no
                            # previous depth, update the depths. This ensures that
                            # when we discover a node we will have the shortest
                            # depth available.
                            #
                            # NOTE: No effort is made to traverse from nodes that
                            # were previously selected if they become reachable via
                            # a shorter path via nodes selected later. This is
                            # currently "intended", but may be worth experimenting
                            # with.
                            depths[adjacent.target_content_id] = next_depth
                helper.add_candidates(new_candidates)

        return self._nodes_with_ids(helper.selected_ids)

    def traversal_search(
        self,
        query: str,
        *,
        k: int = 4,
        depth: int = 1,
        metadata_filter: dict[str, Any] = {},  # noqa: B006
    ) -> Iterable[Node]:
        """Retrieve documents from this knowledge store.

        First, `k` nodes are retrieved using a vector search for the `query` string.
        Then, additional nodes are discovered up to the given `depth` from those
        starting nodes.

        Args:
            query: The query string.
            k: The number of Documents to return from the initial vector search.
                Defaults to 4.
            depth: The maximum depth of edges to traverse. Defaults to 1.
            metadata_filter: Optional metadata to filter the results.

        Returns:
            Collection of retrieved documents.
        """
        # Depth 0:
        #   Query for `k` nodes similar to the question.
        #   Retrieve `content_id` and `link_to_tags`.
        #
        # Depth 1:
        #   Query for nodes that have an incoming tag in the `link_to_tags` set.
        #   Combine node IDs.
        #   Query for `link_to_tags` of those "new" node IDs.
        #
        # ...

        traversal_query = self._get_search_cql(
            columns="content_id, link_to_tags",
            has_limit=True,
            metadata_keys=list(metadata_filter.keys()),
            has_embedding=True,
        )

        visit_nodes_query = self._get_search_cql(
            columns="content_id AS target_content_id",
            has_link_from_tags=True,
            metadata_keys=list(metadata_filter.keys()),
        )

        with self._concurrent_queries() as cq:
            # Map from visited ID to depth
            visited_ids: dict[str, int] = {}

            # Map from visited tag `(kind, tag)` to depth. Allows skipping queries
            # for tags that we've already traversed.
            visited_tags: dict[tuple[str, str], int] = {}

            def visit_nodes(d: int, nodes: Sequence[Any]) -> None:
                nonlocal visited_ids
                nonlocal visited_tags

                # Visit nodes at the given depth.
                # Each node has `content_id` and `link_to_tags`.

                # Iterate over nodes, tracking the *new* outgoing kind tags for this
                # depth. This is tags that are either new, or newly discovered at a
                # lower depth.
                outgoing_tags = set()
                for node in nodes:
                    content_id = node.content_id

                    # Add visited ID. If it is closer it is a new node at this depth:
                    if d <= visited_ids.get(content_id, depth):
                        visited_ids[content_id] = d

                        # If we can continue traversing from this node,
                        if d < depth and node.link_to_tags:
                            # Record any new (or newly discovered at a lower depth)
                            # tags to the set to traverse.
                            for kind, value in node.link_to_tags:
                                if d <= visited_tags.get((kind, value), depth):
                                    # Record that we'll query this tag at the
                                    # given depth, so we don't fetch it again
                                    # (unless we find it an earlier depth)
                                    visited_tags[(kind, value)] = d
                                    outgoing_tags.add((kind, value))

                if outgoing_tags:
                    # If there are new tags to visit at the next depth, query for the
                    # node IDs.
                    for kind, value in outgoing_tags:
                        params = self._get_search_params(
                            link_from_tags=(kind, value), metadata=metadata_filter
                        )
                        cq.execute(
                            query=visit_nodes_query,
                            parameters=params,
                            callback=lambda rows, d=d: visit_targets(d, rows),
                        )

            def visit_targets(d: int, targets: Sequence[Any]) -> None:
                nonlocal visited_ids

                # target_content_id, tag=(kind,value)
                new_nodes_at_next_depth = set()
                for target in targets:
                    content_id = target.target_content_id
                    if d < visited_ids.get(content_id, depth):
                        new_nodes_at_next_depth.add(content_id)

                if new_nodes_at_next_depth:
                    for node_id in new_nodes_at_next_depth:
                        cq.execute(
                            self._query_ids_and_link_to_tags_by_id,
                            parameters=(node_id,),
                            callback=lambda rows, d=d: visit_nodes(d + 1, rows),
                        )

            query_embedding = self._embedding.embed_query(query)
            params = self._get_search_params(
                limit=k,
                metadata=metadata_filter,
                embedding=query_embedding,
            )

            cq.execute(
                traversal_query,
                parameters=params,
                callback=lambda nodes: visit_nodes(0, nodes),
            )

        return self._nodes_with_ids(visited_ids.keys())

    def similarity_search(
        self,
        embedding: list[float],
        k: int = 4,
        metadata_filter: dict[str, Any] = {},  # noqa: B006
    ) -> Iterable[Node]:
        """Retrieve nodes similar to the given embedding, optionally filtered by metadata."""  # noqa: E501
        query, params = self._get_search_cql_and_params(
            embedding=embedding, limit=k, metadata=metadata_filter
        )

        for row in self._session.execute(query, params):
            yield _row_to_node(row)

    def metadata_search(
        self,
        metadata: dict[str, Any] = {},  # noqa: B006
        n: int = 5,
    ) -> Iterable[Node]:
        """Retrieve nodes based on their metadata."""
        query, params = self._get_search_cql_and_params(metadata=metadata, limit=n)

        for row in self._session.execute(query, params):
            yield _row_to_node(row)

    def get_node(self, content_id: str) -> Node:
        """Get a node by its id."""
        return self._nodes_with_ids(ids=[content_id])[0]

    def _get_outgoing_tags(
        self,
        source_ids: Iterable[str],
    ) -> set[tuple[str, str]]:
        """Return the set of outgoing tags for the given source ID(s).

        Args:
            source_ids: The IDs of the source nodes to retrieve outgoing tags for.
        """
        tags = set()

        def add_sources(rows: Iterable[Any]) -> None:
            for row in rows:
                if row.link_to_tags:
                    tags.update(row.link_to_tags)

        with self._concurrent_queries() as cq:
            for source_id in source_ids:
                cq.execute(
                    self._query_ids_and_link_to_tags_by_id,
                    (source_id,),
                    callback=add_sources,
                )

        return tags

    def _get_adjacent(
        self,
        tags: set[tuple[str, str]],
        adjacent_query: PreparedStatement,
        query_embedding: list[float],
        k_per_tag: int | None = None,
        metadata_filter: dict[str, Any] | None = None,
    ) -> Iterable[_Edge]:
        """Return the target nodes with incoming links from any of the given tags.

        Args:
            tags: The tags to look for links *from*.
            adjacent_query: Prepared query for adjacent nodes.
            query_embedding: The query embedding. Used to rank target nodes.
            k_per_tag: The number of target nodes to fetch for each outgoing tag.
            metadata_filter: Optional metadata to filter the results.

        Returns:
            List of adjacent edges.
        """
        targets: dict[str, _Edge] = {}

        def add_targets(rows: Iterable[Any]) -> None:
            # TODO: Figure out how to use the "kind" on the edge.
            # This is tricky, since we currently issue one query for anything
            # adjacent via any kind, and we don't have enough information to
            # determine which kind(s) a given target was reached from.
            for row in rows:
                if row.content_id not in targets:
                    targets[row.content_id] = _Edge(
                        target_content_id=row.content_id,
                        target_text_embedding=row.text_embedding,
                        target_link_to_tags=set(row.link_to_tags or []),
                    )

        with self._concurrent_queries() as cq:
            for kind, value in tags:
                params = self._get_search_params(
                    limit=k_per_tag or 10,
                    metadata=metadata_filter,
                    embedding=query_embedding,
                    link_from_tags=(kind, value),
                )

                cq.execute(
                    query=adjacent_query,
                    parameters=params,
                    callback=add_targets,
                )

        # TODO: Consider a combined limit based on the similarity and/or
        # predicated MMR score?
        return targets.values()

    @staticmethod
    def _normalize_metadata_indexing_policy(
        metadata_indexing: tuple[str, Iterable[str]] | str,
    ) -> MetadataIndexingPolicy:
        mode: MetadataIndexingMode
        fields: set[str]
        # metadata indexing policy normalization:
        if isinstance(metadata_indexing, str):
            if metadata_indexing.lower() == "all":
                mode, fields = (MetadataIndexingMode.DEFAULT_TO_SEARCHABLE, set())
            elif metadata_indexing.lower() == "none":
                mode, fields = (MetadataIndexingMode.DEFAULT_TO_UNSEARCHABLE, set())
            else:
                raise ValueError(
                    f"Unsupported metadata_indexing value '{metadata_indexing}'"
                )
        else:
            if len(metadata_indexing) != 2:  # noqa: PLR2004
                raise ValueError(
                    f"Unsupported metadata_indexing value '{metadata_indexing}'."
                )
            # it's a 2-tuple (mode, fields) still to normalize
            _mode, _field_spec = metadata_indexing
            fields = {_field_spec} if isinstance(_field_spec, str) else set(_field_spec)
            if _mode.lower() in {
                "default_to_unsearchable",
                "allowlist",
                "allow",
                "allow_list",
            }:
                mode = MetadataIndexingMode.DEFAULT_TO_UNSEARCHABLE
            elif _mode.lower() in {
                "default_to_searchable",
                "denylist",
                "deny",
                "deny_list",
            }:
                mode = MetadataIndexingMode.DEFAULT_TO_SEARCHABLE
            else:
                raise ValueError(
                    f"Unsupported metadata indexing mode specification '{_mode}'"
                )
        return (mode, fields)

    @staticmethod
    def _coerce_string(value: Any) -> str:
        if isinstance(value, str):
            return value
        if isinstance(value, bool):
            # bool MUST come before int in this chain of ifs!
            return json.dumps(value)
        if isinstance(value, int):
            # we don't want to store '1' and '1.0' differently
            # for the sake of metadata-filtered retrieval:
            return json.dumps(float(value))
        if isinstance(value, float) or value is None:
            return json.dumps(value)
        # when all else fails ...
        return str(value)

    def _extract_where_clause_cql(
        self,
        has_id: bool = False,
        metadata_keys: Sequence[str] = (),
        has_link_from_tags: bool = False,
    ) -> str:
        wc_blocks: list[str] = []

        if has_id:
            wc_blocks.append("content_id == ?")

        if has_link_from_tags:
            wc_blocks.append("link_from_tags CONTAINS (?, ?)")

        for key in sorted(metadata_keys):
            if _is_metadata_field_indexed(key, self._metadata_indexing_policy):
                wc_blocks.append(f"metadata_s['{key}'] = ?")
            else:
                raise ValueError(
                    "Non-indexed metadata fields cannot be used in queries."
                )

        if len(wc_blocks) == 0:
            return ""

        return " WHERE " + " AND ".join(wc_blocks)

    def _extract_where_clause_params(
        self,
        metadata: dict[str, Any],
        link_from_tags: tuple[str, str] | None = None,
    ) -> list[Any]:
        params: list[Any] = []

        if link_from_tags is not None:
            params.append(link_from_tags[0])
            params.append(link_from_tags[1])

        for key, value in sorted(metadata.items()):
            if _is_metadata_field_indexed(key, self._metadata_indexing_policy):
                params.append(self._coerce_string(value=value))
            else:
                raise ValueError(
                    "Non-indexed metadata fields cannot be used in queries."
                )

        return params

    def _get_search_cql(
        self,
        has_limit: bool = False,
        columns: str | None = CONTENT_COLUMNS,
        metadata_keys: Sequence[str] = (),
        has_id: bool = False,
        has_embedding: bool = False,
        has_link_from_tags: bool = False,
    ) -> PreparedStatement:
        where_clause = self._extract_where_clause_cql(
            has_id=has_id,
            metadata_keys=metadata_keys,
            has_link_from_tags=has_link_from_tags,
        )
        limit_clause = " LIMIT ?" if has_limit else ""
        order_clause = " ORDER BY text_embedding ANN OF ?" if has_embedding else ""

        select_cql = SELECT_CQL_TEMPLATE.format(
            columns=columns,
            table_name=self.table_name(),
            where_clause=where_clause,
            order_clause=order_clause,
            limit_clause=limit_clause,
        )

        if select_cql in self._prepared_query_cache:
            return self._prepared_query_cache[select_cql]

        prepared_query = self._session.prepare(select_cql)
        prepared_query.consistency_level = ConsistencyLevel.ONE
        self._prepared_query_cache[select_cql] = prepared_query

        return prepared_query

    def _get_search_params(
        self,
        limit: int | None = None,
        metadata: dict[str, Any] | None = None,
        embedding: list[float] | None = None,
        link_from_tags: tuple[str, str] | None = None,
    ) -> tuple[PreparedStatement, tuple[Any, ...]]:
        where_params = self._extract_where_clause_params(
            metadata=metadata or {}, link_from_tags=link_from_tags
        )

        limit_params = [limit] if limit is not None else []
        order_params = [embedding] if embedding is not None else []

        return tuple(list(where_params) + order_params + limit_params)

    def _get_search_cql_and_params(
        self,
        limit: int | None = None,
        columns: str | None = CONTENT_COLUMNS,
        metadata: dict[str, Any] | None = None,
        embedding: list[float] | None = None,
        link_from_tags: tuple[str, str] | None = None,
    ) -> tuple[PreparedStatement, tuple[Any, ...]]:
        query = self._get_search_cql(
            has_limit=limit is not None,
            columns=columns,
            metadata_keys=list(metadata.keys()) if metadata else (),
            has_embedding=embedding is not None,
            has_link_from_tags=link_from_tags is not None,
        )
        params = self._get_search_params(
            limit=limit,
            metadata=metadata,
            embedding=embedding,
            link_from_tags=link_from_tags,
        )
        return query, params
