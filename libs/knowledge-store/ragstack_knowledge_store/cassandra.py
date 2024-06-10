import secrets
from dataclasses import dataclass
from typing import (
    Any,
    Iterable,
    List,
    NamedTuple,
    Optional,
    Sequence,
    Type,
)

import numpy as np
from cassandra.cluster import ConsistencyLevel, ResponseFuture, Session
from cassio.config import check_resolve_keyspace, check_resolve_session
from langchain_community.utilities.cassandra import SetupMode
from langchain_community.utils.math import cosine_similarity
from langchain_core.documents import Document
from langchain_core.embeddings import Embeddings

from ragstack_knowledge_store.edge_extractor import EdgeExtractor

from ._utils import strict_zip
from .base import KnowledgeStore, Node, TextNode
from .concurrency import ConcurrentQueries
from .content import Kind

CONTENT_ID = "content_id"


def _row_to_document(row) -> Document:
    return Document(
        page_content=row.text_content,
        metadata={
            CONTENT_ID: row.content_id,
            "kind": row.kind,
        },
    )


def _results_to_documents(results: Optional[ResponseFuture]) -> Iterable[Document]:
    if results:
        for row in results:
            yield _row_to_document(row)


def _results_to_ids(results: Optional[ResponseFuture]) -> Iterable[str]:
    if results:
        for row in results:
            yield row.content_id


def emb_to_ndarray(embedding: List[float]) -> np.ndarray:
    embedding = np.array(embedding, dtype=np.float32)
    if embedding.ndim == 1:
        embedding = np.expand_dims(embedding, axis=0)
    return embedding


@dataclass
class _Candidate:
    score: float
    similarity_to_query: float
    """Lambda * Similarity to the question."""

    embedding: np.ndarray
    """Embedding used for updating similarity to selections."""

    redundancy: float
    """(1 - Lambda) * max(Similarity to selected items)."""

    def __init__(
        self, embedding: List[float], lambda_mult: float, query_embedding: np.ndarray
    ):
        self.embedding = emb_to_ndarray(embedding)

        # TODO: Refactor to use cosine_similarity_top_k to allow an array of embeddings?
        self.similarity_to_query = (
            lambda_mult * cosine_similarity(query_embedding, self.embedding)[0]
        )
        self.redundancy = 0.0
        self.score = self.similarity_to_query - self.redundancy
        self.distance = 0

    def update_for_selection(
        self, lambda_mult: float, selection_embedding: List[float]
    ):
        selected_r_sim = (1 - lambda_mult) * cosine_similarity(
            selection_embedding, self.embedding
        )[0]
        if selected_r_sim > self.redundancy:
            self.redundancy = selected_r_sim
            self.score = self.similarity_to_query - selected_r_sim


class CassandraKnowledgeStore(KnowledgeStore):
    def __init__(
        self,
        embedding: Embeddings,
        edge_extractors: List[EdgeExtractor],
        *,
        node_table: str = "knowledge_nodes",
        edge_table: str = "knowledge_edges",
        session: Optional[Session] = None,
        keyspace: Optional[str] = None,
        setup_mode: SetupMode = SetupMode.SYNC,
        concurrency: int = 20,
    ):
        """A hybrid vector-and-graph knowledge store backed by Cassandra.

        Document chunks support vector-similarity search as well as edges linking
        documents based on structural and semantic properties.

        Parameters configure the ways that edges should be added between
        documents. Many take `Union[bool, Set[str]]`, with `False` disabling
        inference, `True` enabling it globally between all documents, and a set
        of metadata fields defining a scope in which to enable it. Specifically,
        passing a set of metadata fields such as `source` only links documents
        with the same `source` metadata value.

        Args:
            embedding: The embeddings to use for the document content.
            edge_extractors: Edge extractors to use for linking knowledge chunks.
            concurrency: Maximum number of queries to have concurrently executing.
            apply_schema: If true, the schema will be created if necessary. If false,
                the schema must have already been applied.
        """
        session = check_resolve_session(session)
        keyspace = check_resolve_keyspace(keyspace)

        self._concurrency = concurrency
        self._embedding = embedding
        self._node_table = node_table
        self._edge_table = edge_table
        self._session = session
        self._keyspace = keyspace

        if setup_mode == SetupMode.SYNC:
            self._apply_schema()
        elif setup_mode != SetupMode.OFF:
            raise ValueError(
                f"Invalid setup mode {setup_mode.name}. "
                "Only SYNC and OFF are supported at the moment"
            )

        # Ensure the edge extractor `kind`s are unique.
        assert len(edge_extractors) == len(set([e.kind for e in edge_extractors]))
        self._edge_extractors = edge_extractors

        # TODO: Metadata
        # TODO: Parent ID / source ID / etc.
        self._insert_passage = session.prepare(
            f"""
            INSERT INTO {keyspace}.{node_table} (
                content_id, kind, text_content, text_embedding, tags
            ) VALUES (?, '{Kind.passage}', ?, ?, ?)
            """
        )

        self._insert_edge = session.prepare(
            f"""
            INSERT INTO {keyspace}.{edge_table} (
                source_content_id, target_content_id, kind, target_text_embedding
            ) VALUES (?, ?, ?, ?)
            """
        )

        self._query_by_id = session.prepare(
            f"""
            SELECT content_id, kind, text_content
            FROM {keyspace}.{node_table}
            WHERE content_id = ?
            """
        )

        self._query_embedding_by_id = session.prepare(
            f"""
            SELECT content_id, text_embedding
            FROM {keyspace}.{node_table}
            WHERE content_id = ?
            """
        )

        self._query_by_embedding = session.prepare(
            f"""
            SELECT content_id, kind, text_content
            FROM {keyspace}.{node_table}
            ORDER BY text_embedding ANN OF ?
            LIMIT ?
            """
        )
        self._query_by_embedding.consistency_level = ConsistencyLevel.QUORUM

        self._query_ids_by_embedding = session.prepare(
            f"""
            SELECT content_id
            FROM {keyspace}.{node_table}
            ORDER BY text_embedding ANN OF ?
            LIMIT ?
            """
        )
        self._query_ids_by_embedding.consistency_level = ConsistencyLevel.QUORUM

        self._query_ids_and_embedding_by_embedding = session.prepare(
            f"""
            SELECT content_id, text_embedding
            FROM {keyspace}.{node_table}
            ORDER BY text_embedding ANN OF ?
            LIMIT ?
            """
        )
        self._query_ids_and_embedding_by_embedding.consistency_level = (
            ConsistencyLevel.QUORUM
        )

        self._query_linked_ids = session.prepare(
            f"""
            SELECT target_content_id AS content_id
            FROM {keyspace}.{edge_table}
            WHERE source_content_id = ?
            """
        )

        self._query_edges_by_source = session.prepare(
            f"""
            SELECT target_content_id, target_text_embedding
            FROM {keyspace}.{edge_table}
            WHERE source_content_id = ?
            """
        )

        self._query_ids_by_tag = session.prepare(
            f"""
            SELECT content_id
            FROM {keyspace}.{node_table}
            WHERE tags CONTAINS ?
            """
        )

        self._query_ids_and_embedding_by_tag = session.prepare(
            f"""
            SELECT content_id, text_embedding
            FROM {keyspace}.{node_table}
            WHERE tags CONTAINS ?
            """
        )

    def _apply_schema(self):
        """Apply the schema to the database."""
        embedding_dim = len(self._embedding.embed_query("Test Query"))
        self._session.execute(
            f"""CREATE TABLE IF NOT EXISTS {self._keyspace}.{self._node_table} (
                content_id TEXT,
                kind TEXT,
                text_content TEXT,
                text_embedding VECTOR<FLOAT, {embedding_dim}>,

                tags SET<TEXT>,

                PRIMARY KEY (content_id)
            )
            """
        )

        self._session.execute(
            f"""CREATE TABLE IF NOT EXISTS {self._keyspace}.{self._edge_table} (
                source_content_id TEXT,
                target_content_id TEXT,

                -- Kind of edge.
                kind TEXT,

                -- text_embedding of target node. allows MMR to be applied without fetching nodes.
                target_text_embedding VECTOR<FLOAT, {embedding_dim}>,

                PRIMARY KEY (source_content_id, target_content_id)
            )
            """
        )

        # Index on text_embedding (for similarity search)
        self._session.execute(
            f"""CREATE CUSTOM INDEX IF NOT EXISTS {self._node_table}_text_embedding_index
            ON {self._keyspace}.{self._node_table}(text_embedding)
            USING 'StorageAttachedIndex';
            """
        )

        # Index on tags
        self._session.execute(
            f"""
            CREATE CUSTOM INDEX IF NOT EXISTS {self._node_table}_tags_index
            ON {self._keyspace}.{self._node_table} (tags)
            USING 'StorageAttachedIndex';
            """
        )

    @property
    def embeddings(self) -> Optional[Embeddings]:
        return self._embedding

    def _concurrent_queries(self) -> ConcurrentQueries:
        return ConcurrentQueries(self._session, concurrency=self._concurrency)

    # TODO: Async (aadd_nodes)
    def add_nodes(
        self,
        nodes: Iterable[Node] = None,
        **kwargs: Any,
    ):
        texts = []
        metadatas = []
        for node in nodes:
            if not isinstance(node, TextNode):
                raise ValueError("Only adding TextNode is supported at the moment")
            texts.append(node.text)
            metadatas.append(node.metadata)

        text_embeddings = self._embedding.embed_documents(texts)

        ids = []
        with self._concurrent_queries() as cq:
            tuples = strict_zip(texts, text_embeddings, metadatas)
            for text, text_embedding, metadata in tuples:
                if CONTENT_ID not in metadata:
                    metadata[CONTENT_ID] = secrets.token_hex(8)
                id = metadata[CONTENT_ID]
                ids.append(id)

                tags = set()
                tags.update(*[e.tags(text, metadata) for e in self._edge_extractors])

                cq.execute(self._insert_passage, (id, text, text_embedding, tags))

        for extractor in self._edge_extractors:
            extractor.extract_edges(self, texts, text_embeddings, metadatas)

        return ids

    @classmethod
    def from_texts(
        cls: Type["CassandraKnowledgeStore"],
        texts: Iterable[str],
        embedding: Embeddings,
        metadatas: Optional[List[dict]] = None,
        ids: Optional[Iterable[str]] = None,
        **kwargs: Any,
    ) -> "CassandraKnowledgeStore":
        """Return CassandraKnowledgeStore initialized from texts and embeddings."""
        store = cls(embedding, **kwargs)
        store.add_texts(texts, metadatas, ids=ids)
        return store

    @classmethod
    def from_documents(
        cls: Type["CassandraKnowledgeStore"],
        documents: Iterable[Document],
        embedding: Embeddings,
        ids: Optional[Iterable[str]] = None,
        **kwargs: Any,
    ) -> "CassandraKnowledgeStore":
        """Return CassandraKnowledgeStore initialized from documents and embeddings."""
        store = cls(embedding, **kwargs)
        store.add_documents(documents, ids=ids)
        return store

    def similarity_search(
        self, query: str, k: int = 4, **kwargs: Any
    ) -> List[Document]:
        embedding_vector = self._embedding.embed_query(query)
        return self.similarity_search_by_vector(
            embedding_vector,
            k=k,
        )

    def similarity_search_by_vector(
        self, embedding: List[float], k: int = 4, **kwargs: Any
    ) -> List[Document]:
        results = self._session.execute(self._query_by_embedding, (embedding, k))
        return list(_results_to_documents(results))

    def _query_by_ids(
        self,
        ids: Iterable[str],
    ) -> Iterable[Document]:
        results = []
        with self._concurrent_queries() as cq:

            def add_documents(rows, index):
                results.extend([(index, _row_to_document(row)) for row in rows])

            for index, id in enumerate(ids):
                cq.execute(
                    self._query_by_id,
                    parameters=(id,),
                    callback=lambda rows, index=index: add_documents(rows, index),
                )

        results.sort(key=lambda tuple: tuple[0])
        return [doc for _, doc in results]

    def _linked_ids(
        self,
        source_id: str,
    ) -> Iterable[str]:
        results = self._session.execute(self._query_linked_ids, (source_id,))
        return _results_to_ids(results)

    def mmr_traversal_search(
        self,
        query: str,
        *,
        k: int = 4,
        depth: int = 2,
        fetch_k: int = 100,
        lambda_mult: float = 0.5,
        score_threshold: float = float('-inf'),
    ) -> Iterable[Document]:
        """Retrieve documents from this knowledge store using MMR-traversal.

        This strategy first retrieves the top `fetch_k` results by similarity to
        the question. It then selects the top `k` results based on
        maximum-marginal relevance using the given `lambda_mult`.

        At each step, it considers the (remaining) documents from `fetch_k` as
        well as any documents connected by edges to a selected document
        retrieved based on similarity (a "root").

        Args:
            query: The query string to search for.
            k: Number of Documents to return. Defaults to 4.
            fetch_k: Number of Documents to fetch via similarity.
                Defaults to 10.
            depth: Maximum depth of a node (number of edges) from a node
                retrieved via similarity. Defaults to 2.
            lambda_mult: Number between 0 and 1 that determines the degree
                of diversity among the results with 0 corresponding to maximum
                diversity and 1 to minimum diversity. Defaults to 0.5.
            score_threshold: Only documents with a score greater than or equal
                this threshold will be chosen. Defaults to -infinity.
        """
        selected_ids = []
        selected_set = set()

        selected_embeddings = (
            []
        )  # selected embeddings. saved to compute redundancy of new nodes.

        query_embedding = self._embedding.embed_query(query)
        fetched = self._session.execute(
            self._query_ids_and_embedding_by_embedding,
            (query_embedding, fetch_k),
        )

        query_embedding = emb_to_ndarray(query_embedding)
        unselected = {
            row.content_id: _Candidate(row.text_embedding, lambda_mult, query_embedding)
            for row in fetched
        }
        best_score, next_id = max(
            [(u.score, content_id) for (content_id, u) in unselected.items()]
        )

        while len(selected_ids) < k and next_id is not None:
            if best_score < score_threshold:
                break
            selected_id = next_id
            selected_set.add(next_id)
            selected_ids.append(next_id)

            next_selected = unselected.pop(selected_id)
            selected_embedding = next_selected.embedding
            selected_embeddings.append(selected_embedding)

            best_score = float('-inf')
            next_id = None

            # Update unselected scores.
            for content_id, candidate in unselected.items():
                candidate.update_for_selection(lambda_mult, selected_embedding)
                if candidate.score > best_score:
                    best_score = candidate.score
                    next_id = content_id

            # Add unselected edges if reached nodes are within `depth`:
            next_depth = next_selected.distance + 1
            if next_depth < depth:
                adjacents = self._session.execute(
                    self._query_edges_by_source, (selected_id,)
                )
                for row in adjacents:
                    target_id = row.target_content_id
                    if target_id in selected_set:
                        # The adjacent node is already included.
                        continue

                    if target_id in unselected:
                        # The adjancent node is already in the pending set.
                        # Update the distance if we found a shorter path to it.
                        if next_depth < unselected[target_id].distance:
                            unselected[target_id].distance = next_depth
                        continue

                    adjacent = _Candidate(
                        row.target_text_embedding, lambda_mult, query_embedding
                    )
                    for selected_embedding in selected_embeddings:
                        adjacent.update_for_selection(lambda_mult, selected_embedding)

                    unselected[target_id] = adjacent
                    if adjacent.score > best_score:
                        best_score = adjacent.score
                        next_id = row.target_content_id

        return self._query_by_ids(selected_ids)

    def traversal_search(
        self, query: str, *, k: int = 4, depth: int = 1
    ) -> Iterable[Document]:
        """Retrieve documents from this knowledge store.

        First, `k` nodes are retrieved using a vector search for the `query` string.
        Then, additional nodes are discovered up to the given `depth` from those starting
        nodes.

        Args:
            query: The query string.
            k: The number of Documents to return from the initial vector search.
                Defaults to 4.
            depth: The maximum depth of edges to traverse. Defaults to 1.
        Returns:
            Collection of retrieved documents.
        """
        with self._concurrent_queries() as cq:
            visited = {}

            def visit(d: int, nodes: Sequence[NamedTuple]):
                nonlocal visited
                for node in nodes:
                    content_id = node.content_id
                    if d <= visited.get(content_id, depth):
                        visited[content_id] = d
                        # We discovered this for the first time, or at a shorter depth.
                        if d + 1 <= depth:
                            cq.execute(
                                self._query_linked_ids,
                                parameters=(content_id,),
                                callback=lambda n, _d=d: visit(_d + 1, n),
                            )

            query_embedding = self._embedding.embed_query(query)
            cq.execute(
                self._query_ids_by_embedding,
                parameters=(query_embedding, k),
                callback=lambda nodes: visit(0, nodes),
            )

        return self._query_by_ids(visited.keys())
