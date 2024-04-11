"""
This module integrates the ColBERT model with token embedding functionalities, offering tools for efficiently
encoding queries and text chunks into dense vector representations. It facilitates semantic search and
retrieval by providing optimized methods for embedding generation and manipulation.

The core component, ColbertTokenEmbeddings, leverages pre-trained ColBERT models to produce embeddings suitable
for high-relevancy retrieval tasks, with support for both CPU and GPU computing environments.
"""

import logging
import uuid
from typing import List, Optional, Union

import torch
import torch.distributed as dist
from colbert.indexing.collection_encoder import CollectionEncoder
from colbert.infra import ColBERTConfig, Run, RunConfig
from colbert.modeling.checkpoint import Checkpoint
from colbert.modeling.tokenization import QueryTokenizer
from torch import Tensor

from .constant import DEFAULT_COLBERT_MODEL, MAX_MODEL_TOKENS
from .distributed import Distributed, reconcile_nranks
from .runner import Runner
from .token_embedding import EmbeddedChunk, TokenEmbeddings


def calculate_query_maxlen(tokens: List[List[str]]) -> int:
    """
    Calculates an appropriate maximum query length for token embeddings, based on the length of the tokenized input.

    Parameters:
        tokens (List[List[str]]): A nested list where each sublist contains tokens from a single query or chunk.

    Returns:
        int: The calculated maximum length for query tokens, adhering to the specified minimum and maximum bounds,
             and adjusted to the nearest power of two.
    """

    max_token_length = max(len(inner_list) for inner_list in tokens)

    # tokens from the query tokenizer does not include the SEP, CLS
    # SEP, CLS, and Q tokens are added to the query
    # although there could be more SEP tokens if there are more than one sentences, we only add one
    return max_token_length + 3


class ColbertTokenEmbeddings(TokenEmbeddings):
    """
    A class for generating token embeddings using a ColBERT model. This class provides functionalities for
    encoding queries and document chunks into dense vector representations, facilitating semantic search and
    retrieval tasks. It leverages a pre-trained ColBERT model and supports distributed computing environments.

    The class supports both GPU and CPU operations, with GPU usage recommended for performance efficiency.

    Attributes:
        colbert_config (ColBERTConfig): Configuration parameters for the Colbert model.
        checkpoint (Checkpoint): Manages the loading of the model and its parameters.
        encoder (CollectionEncoder): Facilitates the encoding of texts into embeddings.
        query_tokenizer (QueryTokenizer): Tokenizes queries for embedding.
    """

    colbert_config: ColBERTConfig
    checkpoint: Checkpoint
    encoder: CollectionEncoder
    query_tokenizer: QueryTokenizer

    """
    checkpoint is the where the ColBERT model can be specified or downloaded from huggingface
    colbert_model_url overwrites the checkpoint value if it exists
    doc_maxlen is the number tokens each passage is truncated to
    nbits is the number bits that each dimension encodes to
    kmeans_niters specifies the number of iterations of kmeans clustering
    nrank is the number of processors embeddings can run on
          under the default value of -1, the program runs on all available GPUs under CUDA
    query_maxlen is the fixed length of the tokens for query/recall encoding. Anything less will be padded.
    """

    def __init__(
        self,
        checkpoint: str = DEFAULT_COLBERT_MODEL,
        doc_maxlen: int = 220,
        nbits: int = 2,
        kmeans_niters: int = 4,
        nranks: int = -1,
        query_maxlen: int = 32,
        verbose: int = 3,  # 3 is the default on ColBERT checkpoint
        distributed_communication: bool = False,
        **kwargs,
    ):
        """
        Initializes a new instance of the ColbertTokenEmbeddings class, setting up the model configuration,
        loading the necessary checkpoints, and preparing the tokenizer and encoder.

        Parameters:
            checkpoint (str): Path or URL to the Colbert model checkpoint. Default is a pre-defined model.
            doc_maxlen (int): Maximum number of tokens for document chunks.
            nbits (int): The number bits that each dimension encodes to.
            kmeans_niters (int): Number of iterations for k-means clustering during quantization.
            nranks (int): Number of ranks (processors) to use for distributed computing; -1 uses all available CPUs/GPUs.
            query_maxlen (int): Maximum length of query tokens for embedding.
            verbose (int): Verbosity level for logging.
            distributed_communication (bool): Flag to enable distributed computation.
            **kwargs: Additional keyword arguments for future extensions.

        Note:
            This initializer also prepares the system for distributed computation if specified and available.
        """

        self.__cuda = torch.cuda.is_available()
        self.__nranks = reconcile_nranks(nranks)
        total_visible_gpus = torch.cuda.device_count()
        logging.info(f"run nranks {self.__nranks}")
        if (
            self.__nranks > 1
            and not dist.is_initialized()
            and distributed_communication
        ):
            logging.warn(f"distribution initialization must complete on {nranks} gpus")
            Distributed(self.__nranks)
            logging.info("distribution initialization completed")

        with Run().context(RunConfig(nranks=nranks)):
            if self.__cuda:
                torch.cuda.empty_cache()
            self.colbert_config = ColBERTConfig(
                doc_maxlen=doc_maxlen,
                nbits=nbits,
                kmeans_niters=kmeans_niters,
                nranks=self.__nranks,
                checkpoint=checkpoint,
                query_maxlen=query_maxlen,
                gpus=total_visible_gpus,
            )
        logging.info("creating checkpoint")
        self.checkpoint = Checkpoint(
            self.colbert_config.checkpoint,
            colbert_config=self.colbert_config,
            verbose=verbose,
        )
        self.encoder = CollectionEncoder(
            config=self.colbert_config, checkpoint=self.checkpoint
        )
        self.query_tokenizer = QueryTokenizer(self.colbert_config)
        if self.__cuda:
            self.checkpoint = self.checkpoint.cuda()

    def embed_chunks(
        self, texts: List[str], doc_id: Optional[str] = None
    ) -> List[EmbeddedChunk]:
        """
        Encodes a list of text chunks into embeddings, returning them as a list of EmbeddedChunk objects.
        Each chunk text is converted into a dense vector representation.

        Parameters:
            texts (List[str]): The list of chunk texts to be embedded.
            doc_id (Optional[str]): An optional document identifier. If not provided, a UUID is generated.

        Returns:
            List[EmbeddedChunk]: A list of EmbeddedChunk objects containing the embeddings and document/chunk identifiers.
        """

        if doc_id is None:
            doc_id = str(uuid.uuid4())

        timeout = 30 + len(texts)

        return self.encode(texts=texts, doc_id=doc_id, timeout=timeout)

    def embed_query(self, query_text: str) -> Tensor:
        """
        Encodes a single query text into its embedding representation, optimized for retrieval tasks.

        Parameters:
            query_text (str): The query text to be encoded.

        Returns:
            Tensor: A tensor representing the encoded query's embedding.

        Note:
            This method does not pad the query text to query_maxlen. Additionally, it does not
            reload the checkpoint, therefore improving embedding speed.
        """

        chunk_embedding = self.encode(texts=[query_text])[0]
        return chunk_embedding.embeddings

    def encode_queries(
        self,
        query: Union[str, List[str]],
        full_length_search: Optional[bool] = False,
        query_maxlen: int = -1,
    ) -> Tensor:
        """
        Encodes one or more queries into dense vector representations. It supports encoding queries to a fixed
        length, adjusting for the maximum token length or padding as necessary. The method is suitable for both
        single and batch query processing, with optional support for full-length search encoding.

        Parameters:
            query (Union[str, List[str]]): A single query string or a list of query strings to be encoded.
            full_length_search (Optional[bool]): If True, encodes queries for full-length search. Defaults to False.
            query_maxlen (int): A fixed length for query token embeddings. If -1, uses a dynamically calculated value.

        Returns:
            Tensor: A tensor containing the encoded queries. If multiple queries are provided, the tensor will
                    contain one row per query.
        """

        queries = query if isinstance(query, list) else [query]
        bsize = 128 if len(queries) > 128 else None

        tokens = self.query_tokenizer.tokenize(queries)
        fixed_length = max(query_maxlen, self.colbert_config.query_maxlen)
        if query_maxlen < 0:
            fixed_length = calculate_query_maxlen(tokens)
        # we only send one query at a time therefore tokens[0]
        logging.info(
            f"{len(tokens[0])} tokens in first query with query_maxlen {fixed_length}"
        )

        self.checkpoint.query_tokenizer.query_maxlen = fixed_length

        # All query embeddings in the ColBERT documentation
        # this name, EQ or Q, maps the exact name in most colBERT papers
        queriesQ = self.checkpoint.queryFromText(
            queries, bsize=bsize, to_cpu=not self.__cuda, full_length_search=full_length_search
        )
        return queriesQ

    def encode_query(
        self,
        query: str,
        full_length_search: Optional[bool] = False,
        query_maxlen: int = -1,
    ) -> Tensor:
        """
        Encodes a single query string into a dense vector representation. This method is optimized for encoding
        individual queries, allowing for control over the encoding length and supporting full-length search encoding.
        The encoded query is adjusted to a specified or default maximum token length.

        Parameters:
            query (str): The query string to encode.
            full_length_search (Optional[bool]): Indicates whether to encode the query for a full-length search.
                                                  Defaults to False.
            query_maxlen (int): The fixed length for the query token embedding. If -1, uses a dynamically calculated value.

        Returns:
            Tensor: A tensor representing the encoded query's embedding.
        """

        queries = self.encode_queries(
            query, full_length_search, query_maxlen=query_maxlen
        )
        return queries[0]

    def encode(
        self,
        texts: List[str],
        doc_id: Optional[str] = None,
        timeout: int = 60,
    ) -> List[EmbeddedChunk]:
        """
        Encodes a list of texts chunks into embeddings, represented as EmbeddedChunk objects. This
        method leverages the ColBERT model's encoding capabilities to convert textual content into
        dense vector representations suitable for semantic search and retrieval applications.

        Parameters:
            texts (List[str]): The list of text chunks to encode.
            doc_id (Optional[str]): An optional identifier for the document from which the chunks are derived.
                                    If not provided, a UUID will be generated.
            timeout (int): The timeout in seconds for the encoding operation. Defaults to 60 seconds.

        Returns:
            List[EmbeddedChunk]: A list of EmbeddedChunk objects containing the embeddings for each chunk text, along
                                  with their associated document and chunk identifiers.
        """

        runner = Runner(self.__nranks)
        return runner.encode(
            self.colbert_config,
            texts,
            doc_id,
            timeout=timeout,
        )
