from __future__ import annotations

from typing import TYPE_CHECKING

from langchain.prompts import PromptTemplate
from langchain.schema.output_parser import StrOutputParser
from nemoguardrails import LLMRails, RailsConfig
from nemoguardrails.actions.actions import ActionResult

from e2e_tests.langchain.rag_application import (
    BASIC_QA_PROMPT,
    SAMPLE_DATA,
)

if TYPE_CHECKING:
    from langchain.llms.base import BaseLLM
    from langchain.schema.retriever import BaseRetriever
    from langchain.schema.vectorstore import VectorStore


def _config(engine, model) -> str:
    return f"""
    models:
      - type: main
        engine: {engine}
        model: {model}
    """


def _colang() -> str:
    return """
    define user express greeting
        "Hi, how are you?"

    define user ask about product
        "What was MyFakeProductForTesting?"
        "When was MyFakeProductForTesting first released?"
        "What capabilities does MyFakeProductForTesting have?"
        "What is MyFakeProductForTesting's best feature?"

    define bot express greeting
        "Hello! I hope to answer all your questions!"

    define flow greeting
        user express greeting
        bot express greeting

    define flow answer product question
        user ask about product
        $answer = execute rag()
        bot $answer
    """


class NeMoRag:
    def __init__(self, retriever) -> None:
        self.retriever = retriever

    async def rag_using_lc(self, context: dict, llm: BaseLLM) -> ActionResult:
        """Defines the custom rag action"""
        user_message = context.get("last_user_message")
        context_updates = {}

        # Use your pre-defined AstraDB Vector Store as the retriever
        relevant_documents = await self.retriever.aget_relevant_documents(user_message)
        relevant_chunks = "\n".join(
            [chunk.page_content for chunk in relevant_documents]
        )

        # Use a custom prompt template
        prompt_template = PromptTemplate.from_template(BASIC_QA_PROMPT)
        input_variables = {"question": user_message, "context": relevant_chunks}

        # Create LCEL chain
        chain = prompt_template | llm | StrOutputParser()
        answer = await chain.ainvoke(input_variables)

        return ActionResult(return_value=answer, context_updates=context_updates)

    def init(self, app: LLMRails) -> None:
        app.register_action(self.rag_using_lc, "rag")


def _try_runnable_rails(config: RailsConfig, retriever: BaseRetriever) -> None:
    # LLM is created internally to rails using the provided config
    rails = LLMRails(config)
    processor = NeMoRag(retriever)
    processor.init(rails)

    response = rails.generate(
        messages=[
            {
                "role": "user",
                "content": "Hi, how are you?",
            }
        ]
    )
    assert "Hello! I hope to answer all your questions" in response["content"]

    response = rails.generate(
        messages=[
            {
                "role": "user",
                "content": "When was MyFakeProductForTesting first released?",
            }
        ]
    )
    assert "2020" in response["content"]


def run_nemo_guardrails(vector_store: VectorStore, config: dict[str, str]) -> None:
    vector_store.add_texts(SAMPLE_DATA)
    retriever = vector_store.as_retriever()

    model_config = _config(config["engine"], config["model"])
    rails_config = RailsConfig.from_content(
        yaml_content=model_config, colang_content=_colang()
    )
    _try_runnable_rails(config=rails_config, retriever=retriever)
