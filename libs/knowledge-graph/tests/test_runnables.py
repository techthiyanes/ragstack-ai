from langchain_core.language_models import BaseChatModel
from ragstack_knowledge_graph.runnables import extract_entities
from ragstack_knowledge_graph.traverse import Node


def test_extract_entities(llm: BaseChatModel) -> None:
    extractor = extract_entities(llm)
    assert extractor.invoke({"question": "Who is Marie Curie?"}) == [
        Node("Marie Curie", "Person")
    ]
