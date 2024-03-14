import logging
from typing import List

import pytest
from langchain import callbacks

from e2e_tests.conftest import (
    set_current_test_info,
    get_required_env,
    get_vector_store_handler,
)
from e2e_tests.langchain.rag_application import (
    run_rag_custom_chain,
    run_conversational_rag,
)
from e2e_tests.langchain.trulens import run_trulens_evaluation
from e2e_tests.test_utils import get_local_resource_path
from e2e_tests.langchain.nemo_guardrails import run_nemo_guardrails

from langchain.chat_models import ChatOpenAI, AzureChatOpenAI, ChatVertexAI, BedrockChat
from langchain.embeddings import (
    OpenAIEmbeddings,
    VertexAIEmbeddings,
    BedrockEmbeddings,
    HuggingFaceInferenceAPIEmbeddings,
)
from langchain.embeddings.azure_openai import AzureOpenAIEmbeddings
from langchain.llms.huggingface_hub import HuggingFaceHub
from langchain_core.embeddings import Embeddings
from langchain_core.messages import HumanMessage
from langchain_core.prompts import ChatPromptTemplate
from langchain_google_genai import ChatGoogleGenerativeAI
from vertexai.vision_models import MultiModalEmbeddingModel, Image

from e2e_tests.test_utils.tracing import record_langsmith_sharelink
from e2e_tests.test_utils.vector_store_handler import VectorStoreImplementation


@pytest.fixture
def astra_db():
    handler = get_vector_store_handler(VectorStoreImplementation.ASTRADB)
    context = handler.before_test()
    yield context
    handler.after_test()


@pytest.fixture
def cassandra():
    handler = get_vector_store_handler(VectorStoreImplementation.CASSANDRA)
    context = handler.before_test()
    yield context
    handler.after_test()


def _chat_openai(**kwargs) -> ChatOpenAI:
    return ChatOpenAI(
        openai_api_key=get_required_env("OPEN_AI_KEY"), temperature=0, **kwargs
    )


@pytest.fixture
def openai_gpt35turbo_llm():
    # NeMo guardrails fails for this model with the given prompts.
    model = "gpt-3.5-turbo"
    return {"llm": _chat_openai(model=model, streaming=False), "nemo_config": None}


@pytest.fixture
def openai_gpt4_llm():
    model = "gpt-4"
    return {
        "llm": _chat_openai(model=model, streaming=False),
        "nemo_config": {"engine": "openai", "model": model},
    }


@pytest.fixture
def openai_gpt4_llm_streaming():
    model = "gpt-4"
    return {
        "llm": _chat_openai(model=model, streaming=True),
        "nemo_config": {"engine": "openai", "model": model},
    }


def _openai_embeddings(**kwargs) -> OpenAIEmbeddings:
    return OpenAIEmbeddings(openai_api_key=get_required_env("OPEN_AI_KEY"), **kwargs)


@pytest.fixture
def openai_ada002_embedding():
    return _openai_embeddings(model="text-embedding-ada-002")


@pytest.fixture
def openai_3small_embedding():
    return _openai_embeddings(model="text-embedding-3-small")


@pytest.fixture
def openai_3large_embedding():
    return _openai_embeddings(model="text-embedding-3-large")


@pytest.fixture
def azure_openai_gpt35turbo_llm():
    # model is configurable because it can be different from the deployment
    # but the targeting model must be gpt-35-turbo
    return {
        "llm": AzureChatOpenAI(
            azure_deployment=get_required_env("AZURE_OPEN_AI_CHAT_MODEL_DEPLOYMENT"),
            openai_api_base=get_required_env("AZURE_OPEN_AI_ENDPOINT"),
            openai_api_key=get_required_env("AZURE_OPEN_AI_KEY"),
            openai_api_type="azure",
            openai_api_version="2023-07-01-preview",
        ),
        "nemo_config": None,
    }


@pytest.fixture
def azure_openai_ada002_embedding():
    # model is configurable because it can be different from the deployment
    # but the targeting model must be ada-002

    model_and_deployment = get_required_env("AZURE_OPEN_AI_EMBEDDINGS_MODEL_DEPLOYMENT")
    return AzureOpenAIEmbeddings(
        model=model_and_deployment,
        deployment=model_and_deployment,
        openai_api_key=get_required_env("AZURE_OPEN_AI_KEY"),
        openai_api_base=get_required_env("AZURE_OPEN_AI_ENDPOINT"),
        openai_api_type="azure",
        openai_api_version="2023-05-15",
        chunk_size=1,
    )


@pytest.fixture
def vertex_bison_llm():
    return {"llm": ChatVertexAI(model_name="chat-bison"), "nemo_config": None}


@pytest.fixture
def vertex_gecko_embedding():
    return VertexAIEmbeddings(model_name="textembedding-gecko")


def _bedrock_chat(**kwargs) -> BedrockChat:
    return BedrockChat(region_name=get_required_env("BEDROCK_AWS_REGION"), **kwargs)


@pytest.fixture
def bedrock_anthropic_claudev2_llm():
    return {
        "llm": _bedrock_chat(
            model_id="anthropic.claude-v2",
        ),
        "nemo_config": None,
    }


@pytest.fixture
def bedrock_mistral_mistral7b_llm():
    return {
        "llm": _bedrock_chat(
            model_id="mistral.mistral-7b-instruct-v0:2",
        ),
        "nemo_config": None,
    }


@pytest.fixture
def bedrock_meta_llama2_llm():
    return {
        "llm": _bedrock_chat(model_id="meta.llama2-13b-chat-v1"),
        "nemo_config": None,
    }


@pytest.fixture
def bedrock_titan_embedding():
    return BedrockEmbeddings(
        model_id="amazon.titan-embed-text-v1",
        region_name=get_required_env("BEDROCK_AWS_REGION"),
    )


@pytest.fixture
def bedrock_cohere_embedding():
    return BedrockEmbeddings(
        model_id="cohere.embed-english-v3",
        region_name=get_required_env("BEDROCK_AWS_REGION"),
    )


@pytest.fixture
def huggingface_hub_flant5xxl_llm():
    return {
        "llm": HuggingFaceHub(
            repo_id="google/flan-t5-xxl",
            huggingfacehub_api_token=get_required_env("HUGGINGFACE_HUB_KEY"),
            model_kwargs={"temperature": 1, "max_length": 256},
        ),
        "nemo_config": None,
    }


@pytest.fixture
def huggingface_hub_minilml6v2_embedding():
    return HuggingFaceInferenceAPIEmbeddings(
        api_key=get_required_env("HUGGINGFACE_HUB_KEY"),
        model_name="sentence-transformers/all-MiniLM-l6-v2",
    )


@pytest.fixture
def nvidia_aifoundation_nvolveqa40k_embedding():
    get_required_env("NVIDIA_API_KEY")
    from langchain_nvidia_ai_endpoints.embeddings import NVIDIAEmbeddings

    return NVIDIAEmbeddings(model="playground_nvolveqa_40k")


@pytest.fixture
def nvidia_aifoundation_mixtral8x7b_llm():
    get_required_env("NVIDIA_API_KEY")
    from langchain_nvidia_ai_endpoints import ChatNVIDIA

    return {"llm": ChatNVIDIA(model="playground_mixtral_8x7b"), "nemo_config": None}


@pytest.mark.parametrize(
    "test_case",
    ["rag_custom_chain", "conversational_rag", "trulens", "nemo_guardrails"],
)
@pytest.mark.parametrize(
    "vector_store",
    ["astra_db", "cassandra"],
)
@pytest.mark.parametrize(
    "embedding,llm",
    [
        ("openai_ada002_embedding", "openai_gpt35turbo_llm"),
        ("openai_3small_embedding", "openai_gpt4_llm"),
        ("openai_3large_embedding", "openai_gpt4_llm_streaming"),
        ("azure_openai_ada002_embedding", "azure_openai_gpt35turbo_llm"),
        ("vertex_gecko_embedding", "vertex_bison_llm"),
        ("bedrock_titan_embedding", "bedrock_anthropic_claudev2_llm"),
        ("bedrock_cohere_embedding", "bedrock_mistral_mistral7b_llm"),
        ("bedrock_cohere_embedding", "bedrock_meta_llama2_llm"),
        ("huggingface_hub_minilml6v2_embedding", "huggingface_hub_flant5xxl_llm"),
        (
            "nvidia_aifoundation_nvolveqa40k_embedding",
            "nvidia_aifoundation_mixtral8x7b_llm",
        ),
    ],
)
def test_rag(test_case, vector_store, embedding, llm, request, record_property):
    set_current_test_info(
        "langchain::" + test_case,
        f"{llm},{embedding},{vector_store}",
    )
    resolved_vector_store = request.getfixturevalue(vector_store)
    resolved_embedding = request.getfixturevalue(embedding)
    resolved_llm = request.getfixturevalue(llm)
    _run_test(
        test_case,
        resolved_vector_store,
        resolved_embedding,
        resolved_llm,
        record_property,
    )


def _run_test(
    test_case: str,
    vector_store_context,
    embedding,
    resolved_llm,
    record_property,
):
    vector_store = vector_store_context.new_langchain_vector_store(embedding=embedding)
    llm = resolved_llm["llm"]
    if test_case == "rag_custom_chain":
        run_rag_custom_chain(
            vector_store=vector_store, llm=llm, record_property=record_property
        )
    elif test_case == "conversational_rag":
        run_conversational_rag(
            vector_store=vector_store,
            llm=llm,
            chat_memory=vector_store_context.new_langchain_chat_memory(),
            record_property=record_property,
        )
        # TODO: Add record property
    elif test_case == "trulens":
        run_trulens_evaluation(vector_store=vector_store, llm=llm)
    elif test_case == "nemo_guardrails":
        config = resolved_llm["nemo_config"]
        if config:
            # NeMo creates the LLM internally using the config
            run_nemo_guardrails(
                vector_store=vector_store,
                config=config,
            )
        else:
            pytest.skip("Skipping NeMo test for this configuration")
    else:
        raise ValueError(f"Unknown test case: {test_case}")


@pytest.fixture
def vertex_gemini_multimodal_embedding():
    return MultiModalEmbeddingModel.from_pretrained("multimodalembedding@001"), 1408


@pytest.fixture
def vertex_gemini_pro_vision_llm():
    return ChatVertexAI(model_name="gemini-pro-vision")


@pytest.fixture
def vertex_gemini_pro_llm():
    return ChatVertexAI(model_name="gemini-pro")


@pytest.fixture
def gemini_pro_vision_llm():
    return ChatGoogleGenerativeAI(
        model="gemini-pro-vision", google_api_key=get_required_env("GOOGLE_API_KEY")
    )


@pytest.fixture
def gemini_pro_llm():
    return ChatGoogleGenerativeAI(
        model="gemini-pro", google_api_key=get_required_env("GOOGLE_API_KEY")
    )


@pytest.mark.parametrize(
    "vector_store",
    ["astra_db", "cassandra"],
)
@pytest.mark.parametrize(
    "embedding,llm",
    [
        # disable due to this bug: https://github.com/googleapis/python-aiplatform/issues/3227
        # ("vertex_gemini_multimodal_embedding", "vertex_gemini_pro_vision_llm"),
        ("vertex_gemini_multimodal_embedding", "gemini_pro_vision_llm"),
    ],
)
def test_multimodal(vector_store, embedding, llm, request, record_property):
    set_current_test_info(
        "langchain::multimodal_rag",
        f"{llm},{embedding},{vector_store}",
    )

    resolved_embedding, embedding_size = request.getfixturevalue(embedding)

    class FakeEmbeddings(Embeddings):
        def embed_documents(self, texts: List[str]) -> List[List[float]]:
            return [[0.0] * embedding_size] * len(texts)

        def embed_query(self, text: str) -> List[float]:
            return [0.0] * embedding_size

    enhanced_vector_store = request.getfixturevalue(
        vector_store
    ).new_langchain_vector_store(embedding=FakeEmbeddings())
    resolved_llm = request.getfixturevalue(llm)

    tree_image = get_local_resource_path("tree.jpeg")
    products = [
        {
            "name": "Coffee Machine Ultra Cool",
            "image": get_local_resource_path("coffee_machine.jpeg"),
        },
        {"name": "Tree", "image": tree_image},
        {"name": "Another Tree", "image": tree_image},
        {"name": "Another Tree 2", "image": tree_image},
        {"name": "Another Tree 3", "image": tree_image},
    ]

    for p in products:
        img = Image.load_from_file(p["image"])
        embeddings = resolved_embedding.get_embeddings(
            image=img, contextual_text=p["name"]
        )
        p["$vector"] = embeddings.image_embedding

        enhanced_vector_store.put_document(
            p["name"], p["name"], {}, embeddings.image_embedding
        )

    query_image_path = get_local_resource_path("coffee_maker_part.png")
    img = Image.load_from_file(query_image_path)
    embeddings = resolved_embedding.get_embeddings(
        image=img, contextual_text="Coffee Maker Part"
    )

    documents = enhanced_vector_store.search_documents(embeddings.image_embedding, 3)
    image_message = {
        "type": "image_url",
        "image_url": {"url": query_image_path},
    }
    docs_str = ", ".join([f"'{p}'" for p in documents])
    prompt = f"Tell me which one of these products it is part of. Only include product from the ones below: {docs_str}."
    logging.info(f"Prompt: {prompt}")

    text_message = {
        "type": "text",
        "text": prompt,
    }
    message = HumanMessage(content=[text_message, image_message])
    with callbacks.collect_runs() as cb:
        response = resolved_llm([message])
        run_id = cb.traced_runs[0].id
        record_langsmith_sharelink(run_id, record_property)
        answer = str(response.content)
        assert (
            "Coffee Machine Ultra Cool" in answer
        ), f"Expected Coffee Machine Ultra Cool in the answer but got: {answer}"


@pytest.mark.parametrize("chat", ["vertex_gemini_pro_llm", "gemini_pro_llm"])
def test_chat(chat, request, record_property):
    set_current_test_info(
        "langchain::chat",
        chat,
    )
    chat_model = request.getfixturevalue(chat)
    prompt = ChatPromptTemplate.from_messages(
        [("human", "Hello! Where Archimede was born?")]
    )
    chain = prompt | chat_model
    with callbacks.collect_runs() as cb:
        response = chain.invoke({})
        run_id = cb.traced_runs[0].id
        record_langsmith_sharelink(run_id, record_property)
        assert "Syracuse" in str(
            response.content
        ), f"Expected Syracuse in the answer but got: {str(response.content)}"
