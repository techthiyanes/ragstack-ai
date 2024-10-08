"""ColBERT Vector Store.

This module provides an implementation of the BaseVectorStore abstract class,
specifically designed for use with a Cassandra database backend.
It allows for the efficient storage and management of text embeddings
generated by a ColBERT model, facilitating scalable and high-relevancy retrieval
operations.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

from typing_extensions import override

from .base_vector_store import BaseVectorStore
from .colbert_retriever import ColbertRetriever
from .objects import Chunk, Metadata

if TYPE_CHECKING:
    from .base_database import BaseDatabase
    from .base_embedding_model import BaseEmbeddingModel
    from .base_retriever import BaseRetriever


class ColbertVectorStore(BaseVectorStore):
    """A vector store implementation for ColBERT.

    Args:
        database (BaseDatabase): The database to use for storage
        embedding_model (Optional[BaseEmbeddingModel]): The embedding model to use
            for embedding text and queries.
    """

    _database: BaseDatabase
    _embedding_model: BaseEmbeddingModel | None

    def __init__(
        self,
        database: BaseDatabase,
        embedding_model: BaseEmbeddingModel | None = None,
    ):
        self._database = database
        self._embedding_model = embedding_model

    def _validate_embedding_model(self) -> BaseEmbeddingModel:
        if self._embedding_model is None:
            msg = "To use this method, `embedding_model` must be set on class creation."
            raise AttributeError(msg)
        return self._embedding_model

    def _build_chunks(
        self,
        texts: list[str],
        metadatas: list[Metadata] | None = None,
        doc_id: str | None = None,
    ) -> list[Chunk]:
        embedding_model = self._validate_embedding_model()

        if metadatas is not None and len(texts) != len(metadatas):
            msg = "Length of texts and metadatas must match."
            raise ValueError(msg)

        if doc_id is None:
            doc_id = str(uuid.uuid4())

        embeddings = embedding_model.embed_texts(texts=texts)

        chunks: list[Chunk] = []
        for i, text in enumerate(texts):
            chunks.append(
                Chunk(
                    doc_id=doc_id,
                    chunk_id=i,
                    text=text,
                    metadata={} if metadatas is None else metadatas[i],
                    embedding=embeddings[i],
                )
            )
        return chunks

    @override
    def add_chunks(self, chunks: list[Chunk]) -> list[tuple[str, int]]:
        return self._database.add_chunks(chunks=chunks)

    @override
    def add_texts(
        self,
        texts: list[str],
        metadatas: list[Metadata] | None = None,
        doc_id: str | None = None,
    ) -> list[tuple[str, int]]:
        chunks = self._build_chunks(texts=texts, metadatas=metadatas, doc_id=doc_id)
        return self._database.add_chunks(chunks=chunks)

    @override
    def delete_chunks(self, doc_ids: list[str]) -> bool:
        return self._database.delete_chunks(doc_ids=doc_ids)

    @override
    async def aadd_chunks(
        self, chunks: list[Chunk], concurrent_inserts: int = 100
    ) -> list[tuple[str, int]]:
        return await self._database.aadd_chunks(
            chunks=chunks, concurrent_inserts=concurrent_inserts
        )

    @override
    async def aadd_texts(
        self,
        texts: list[str],
        metadatas: list[Metadata] | None = None,
        doc_id: str | None = None,
        concurrent_inserts: int = 100,
    ) -> list[tuple[str, int]]:
        chunks = self._build_chunks(texts=texts, metadatas=metadatas, doc_id=doc_id)
        return await self._database.aadd_chunks(
            chunks=chunks, concurrent_inserts=concurrent_inserts
        )

    @override
    async def adelete_chunks(
        self, doc_ids: list[str], concurrent_deletes: int = 100
    ) -> bool:
        return await self._database.adelete_chunks(
            doc_ids=doc_ids, concurrent_deletes=concurrent_deletes
        )

    @override
    def as_retriever(self) -> BaseRetriever:
        embedding_model = self._validate_embedding_model()
        return ColbertRetriever(
            database=self._database, embedding_model=embedding_model
        )
