"""Base Database module.

This module defines abstract base classes for implementing storage mechanisms for
text chunk embeddings, specifically designed to work with ColBERT or similar embedding
models.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .objects import Chunk, Vector


class BaseDatabase(ABC):
    """Base Database abstract class for ColBERT.

    Abstract base class (ABC) for a storage system designed to hold vector
    representations of text chunks, typically generated by a ColBERT model or similar
    embedding model.

    This class defines the interface for storing and managing the embedded text chunks,
    supporting operations like adding new chunks to the store and deleting existing
    documents by their identifiers.
    """

    @abstractmethod
    def add_chunks(self, chunks: list[Chunk]) -> list[tuple[str, int]]:
        """Stores a list of embedded text chunks in the vector store.

        Args:
            chunks (List[Chunk]): A list of `Chunk` instances to be stored.

        Returns:
            a list of tuples: (doc_id, chunk_id)
        """

    @abstractmethod
    def delete_chunks(self, doc_ids: list[str]) -> bool:
        """Deletes chunks from the vector store based on their document id.

        Args:
            doc_ids (List[str]): A list of document identifiers specifying the chunks
                to be deleted.

        Returns:
            True if the all the deletes were successful.
        """

    @abstractmethod
    async def aadd_chunks(
        self, chunks: list[Chunk], concurrent_inserts: int = 100
    ) -> list[tuple[str, int]]:
        """Stores a list of embedded text chunks in the vector store.

        Args:
            chunks: A list of `Chunk` instances to be stored.
            concurrent_inserts: How many concurrent inserts to make to
                the database. Defaults to 100.

        Returns:
            a list of tuples: (doc_id, chunk_id)
        """

    @abstractmethod
    async def adelete_chunks(
        self, doc_ids: list[str], concurrent_deletes: int = 100
    ) -> bool:
        """Deletes chunks from the vector store based on their document id.

        Args:
            doc_ids: A list of document identifiers specifying the chunks
                to be deleted.
            concurrent_deletes: How many concurrent deletes to make
                to the database. Defaults to 100.

        Returns:
            True if the all the deletes were successful.
        """

    @abstractmethod
    async def search_relevant_chunks(self, vector: Vector, n: int) -> list[Chunk]:
        """Retrieves 'n' ANN results for an embedded token vector.

        Returns:
            A list of Chunks with only `doc_id` and `chunk_id` set.
            Fewer than 'n' results may be returned.
        """

    @abstractmethod
    async def get_chunk_embedding(self, doc_id: str, chunk_id: int) -> Chunk:
        """Retrieve the embedding data for a chunk.

        Returns:
            A chunk with `doc_id`, `chunk_id`, and `embedding` set.
        """

    @abstractmethod
    async def get_chunk_data(
        self, doc_id: str, chunk_id: int, include_embedding: bool = False
    ) -> Chunk:
        """Retrieve the text and metadata for a chunk.

        Returns:
            A chunk with `doc_id`, `chunk_id`, `text`, `metadata`, and optionally
            `embedding` set.
        """

    @abstractmethod
    def close(self) -> None:
        """Cleans up any open resources."""
