"""Casandra Database.

This module provides an implementation of the BaseVectorStore abstract class,
specifically designed for use with a Cassandra database backend. It allows for the
efficient storage and management of text embeddings generated by a ColBERT model,
facilitating scalable and high-relevancy retrieval operations.
"""

from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from typing import TYPE_CHECKING, Any, Awaitable

import cassio
from cassio.table.query import Predicate, PredicateOperator
from cassio.table.tables import ClusteredMetadataVectorCassandraTable
from typing_extensions import Self, override

from .base_database import BaseDatabase
from .constant import DEFAULT_COLBERT_DIM
from .objects import Chunk, Vector

if TYPE_CHECKING:
    from cassandra.cluster import Session


class CassandraDatabaseError(Exception):
    """Exception raised for errors in the CassandraDatabase class."""


class CassandraDatabase(BaseDatabase):
    """Casandra Database.

    An implementation of the BaseDatabase abstract base class using Cassandra as the
    backend storage system. This class provides methods to store, retrieve, and manage
    text embeddings within a Cassandra database, specifically designed for handling
    vector embeddings generated by ColBERT.

    The table schema and custom index for ANN queries are automatically created if they
    do not exist.
    """

    _table: ClusteredMetadataVectorCassandraTable

    def __new__(cls) -> Self:  # noqa: D102
        raise ValueError(
            "This class cannot be instantiated directly. "
            "Please use the `from_astra()` or `from_session()` class methods."
        )

    @classmethod
    def from_astra(
        cls,
        database_id: str,
        astra_token: str,
        keyspace: str | None = "default_keyspace",
        table_name: str = "colbert",
        timeout: int | None = 300,
    ) -> Self:
        """Creates a CassandraVectorStore using AstraDB connection info."""
        cassio.init(token=astra_token, database_id=database_id, keyspace=keyspace)
        session = cassio.config.check_resolve_session()
        session.default_timeout = timeout

        return cls.from_session(
            session=session, keyspace=keyspace, table_name=table_name
        )

    @classmethod
    def from_session(
        cls,
        session: Session,
        keyspace: str | None = "default_keyspace",
        table_name: str = "colbert",
    ) -> Self:
        """Creates a CassandraVectorStore using an existing session."""
        instance = super().__new__(cls)
        instance._initialize(session=session, keyspace=keyspace, table_name=table_name)  # noqa: SLF001
        return instance

    def _initialize(
        self,
        session: Session,
        keyspace: str | None,
        table_name: str,
    ) -> None:
        """Initializes a new instance of the CassandraVectorStore.

        Args:
            session: The Cassandra session to use.
            keyspace: The keyspace in which the table exists or will be created.
            table_name: The name of the table to use or create for storing
                embeddings.
            timeout: The default timeout in seconds for Cassandra
                operations. Defaults to 180.
        """
        try:
            is_astra = session.cluster.cloud
        except AttributeError:
            is_astra = False

        logging.info(
            "Cassandra store is running on %s",
            "AstraDB" if is_astra else "Apache Cassandra",
        )

        self._table = ClusteredMetadataVectorCassandraTable(
            session=session,
            keyspace=keyspace,
            table=table_name,
            row_id_type=["INT", "INT"],
            vector_dimension=DEFAULT_COLBERT_DIM,
            vector_source_model="bert" if is_astra else None,
            vector_similarity_function=None if is_astra else "DOT_PRODUCT",
        )

    def _log_insert_error(
        self, doc_id: str, chunk_id: int, embedding_id: int, exp: Exception
    ) -> None:
        if embedding_id == -1:
            logging.error(
                "issue inserting document data: %s chunk: %s: %s", doc_id, chunk_id, exp
            )
        else:
            logging.error(
                "issue inserting document embedding: %s chunk: %s embedding: %s: %s",
                doc_id,
                chunk_id,
                embedding_id,
                exp,
            )

    @override
    def add_chunks(self, chunks: list[Chunk]) -> list[tuple[str, int]]:
        failed_chunks: list[tuple[str, int]] = []
        success_chunks: list[tuple[str, int]] = []

        for chunk in chunks:
            doc_id = chunk.doc_id
            chunk_id = chunk.chunk_id

            try:
                self._table.put(
                    partition_id=doc_id,
                    row_id=(chunk_id, -1),
                    body_blob=chunk.text,
                    metadata=chunk.metadata,
                )
            except Exception as exp:  # noqa: BLE001
                self._log_insert_error(
                    doc_id=doc_id, chunk_id=chunk_id, embedding_id=-1, exp=exp
                )
                failed_chunks.append((doc_id, chunk_id))
                continue

            if chunk.embedding:
                for embedding_id, vector in enumerate(chunk.embedding):
                    try:
                        self._table.put(
                            partition_id=doc_id,
                            row_id=(chunk_id, embedding_id),
                            vector=vector,
                        )
                    except Exception as exp:  # noqa: BLE001
                        self._log_insert_error(
                            doc_id=doc_id, chunk_id=chunk_id, embedding_id=-1, exp=exp
                        )
                        failed_chunks.append((doc_id, chunk_id))
                        continue

            success_chunks.append((doc_id, chunk_id))

        if len(failed_chunks) > 0:
            raise CassandraDatabaseError(
                f"add failed for these chunks: {failed_chunks}. "
                f"See error logs for more info."
            )

        return success_chunks

    async def _limited_put(
        self,
        sem: asyncio.Semaphore,
        doc_id: str,
        chunk_id: int,
        embedding_id: int = -1,
        text: str | None = None,
        metadata: dict[str, Any] | None = None,
        vector: Vector | None = None,
    ) -> tuple[str, int, int, Exception | None]:
        row_id = (chunk_id, embedding_id)
        async with sem:
            try:
                if vector is None:
                    await self._table.aput(
                        partition_id=doc_id,
                        row_id=row_id,
                        body_blob=text,
                        metadata=metadata,
                    )
                else:
                    await self._table.aput(
                        partition_id=doc_id, row_id=row_id, vector=vector
                    )
            except Exception as e:  # noqa: BLE001
                return doc_id, chunk_id, embedding_id, e
            return doc_id, chunk_id, embedding_id, None

    @override
    async def aadd_chunks(
        self, chunks: list[Chunk], concurrent_inserts: int = 100
    ) -> list[tuple[str, int]]:
        semaphore = asyncio.Semaphore(concurrent_inserts)
        all_tasks: list[Awaitable[tuple[str, int, int, Exception | None]]] = []
        tasks_per_chunk: dict[tuple[str, int], int] = defaultdict(int)

        for chunk in chunks:
            doc_id = chunk.doc_id
            chunk_id = chunk.chunk_id
            text = chunk.text
            metadata = chunk.metadata

            all_tasks.append(
                self._limited_put(
                    sem=semaphore,
                    doc_id=doc_id,
                    chunk_id=chunk_id,
                    text=text,
                    metadata=metadata,
                )
            )
            tasks_per_chunk[(doc_id, chunk_id)] += 1

            if chunk.embedding:
                for index, vector in enumerate(chunk.embedding):
                    all_tasks.append(
                        self._limited_put(
                            sem=semaphore,
                            doc_id=doc_id,
                            chunk_id=chunk_id,
                            embedding_id=index,
                            vector=vector,
                        )
                    )
                    tasks_per_chunk[(doc_id, chunk_id)] += 1

        results = await asyncio.gather(*all_tasks, return_exceptions=True)

        for result in results:
            if isinstance(result, BaseException):
                logging.error("issue inserting data", exc_info=result)
            else:
                doc_id, chunk_id, embedding_id, exp = result
                if exp is None:
                    tasks_per_chunk[(doc_id, chunk_id)] -= 1
                else:
                    self._log_insert_error(
                        doc_id=doc_id,
                        chunk_id=chunk_id,
                        embedding_id=embedding_id,
                        exp=exp,
                    )

        outputs: list[tuple[str, int]] = []
        failed_chunks: list[tuple[str, int]] = []

        for doc_id, chunk_id in tasks_per_chunk:
            if tasks_per_chunk[(doc_id, chunk_id)] == 0:
                outputs.append((doc_id, chunk_id))
            else:
                failed_chunks.append((doc_id, chunk_id))

        if len(failed_chunks) > 0:
            raise CassandraDatabaseError(
                f"add failed for these chunks: {failed_chunks}. "
                f"See error logs for more info."
            )

        return outputs

    @override
    def delete_chunks(self, doc_ids: list[str]) -> bool:
        failed_docs: list[str] = []

        for doc_id in doc_ids:
            try:
                self._table.delete_partition(partition_id=doc_id)
            except Exception:
                logging.exception("issue on delete of document: %s", doc_id)
                failed_docs.append(doc_id)

        if len(failed_docs) > 0:
            raise CassandraDatabaseError(
                "delete failed for these docs: %s. See error logs for more info.",
                failed_docs,
            )

        return True

    async def _limited_delete(
        self,
        sem: asyncio.Semaphore,
        doc_id: str,
    ) -> tuple[str, Exception | None]:
        async with sem:
            try:
                await self._table.adelete_partition(partition_id=doc_id)
            except Exception as e:  # noqa: BLE001
                return doc_id, e
            return doc_id, None

    @override
    async def adelete_chunks(
        self, doc_ids: list[str], concurrent_deletes: int = 100
    ) -> bool:
        semaphore = asyncio.Semaphore(concurrent_deletes)
        all_tasks = [
            self._limited_delete(
                sem=semaphore,
                doc_id=doc_id,
            )
            for doc_id in doc_ids
        ]

        results = await asyncio.gather(*all_tasks, return_exceptions=True)

        success = True
        failed_docs: list[str] = []

        for result in results:
            if isinstance(result, BaseException):
                logging.error("issue inserting data", exc_info=result)
            else:
                doc_id, exp = result
                if exp is not None:
                    logging.error("issue deleting document: %s", doc_id, exc_info=exp)
                    success = False
                    failed_docs.append(doc_id)

        if len(failed_docs) > 0:
            raise CassandraDatabaseError(
                f"delete failed for these docs: {failed_docs}. "
                f"See error logs for more info."
            )

        return success

    @override
    async def search_relevant_chunks(self, vector: Vector, n: int) -> list[Chunk]:
        chunks: set[Chunk] = set()

        # TODO: only return partition_id and row_id after cassio supports this
        rows = await self._table.aann_search(vector=vector, n=n)
        for row in rows:
            chunks.add(
                Chunk(
                    doc_id=row["partition_id"],
                    chunk_id=row["row_id"][0],
                )
            )
        return list(chunks)

    @override
    async def get_chunk_embedding(self, doc_id: str, chunk_id: int) -> Chunk:
        row_id = (chunk_id, Predicate(PredicateOperator.GT, -1))
        rows = await self._table.aget_partition(partition_id=doc_id, row_id=row_id)

        embedding = [row["vector"] for row in rows]

        return Chunk(doc_id=doc_id, chunk_id=chunk_id, embedding=embedding)

    @override
    async def get_chunk_data(
        self, doc_id: str, chunk_id: int, include_embedding: bool = False
    ) -> Chunk:
        row_id = (chunk_id, Predicate(PredicateOperator.EQ, -1))
        row = await self._table.aget(partition_id=doc_id, row_id=row_id)

        if row is None:
            raise CassandraDatabaseError(
                f"no chunk found for doc_id: {doc_id} chunk_id: {chunk_id}"
            )

        if include_embedding is True:
            embedded_chunk = await self.get_chunk_embedding(
                doc_id=doc_id, chunk_id=chunk_id
            )
            embedding = embedded_chunk.embedding
        else:
            embedding = None

        return Chunk(
            doc_id=doc_id,
            chunk_id=chunk_id,
            text=row["body_blob"],
            metadata=row["metadata"],
            embedding=embedding,
        )

    @override
    def close(self) -> None:
        pass
