from collections.abc import Iterable

import graphviz
from langchain_community.graphs.graph_document import GraphDocument, Node

from .knowledge_schema import KnowledgeSchema


def _node_label(node: Node) -> str:
    return f"{node.id} [{node.type}]"


def print_graph_documents(
    graph_documents: GraphDocument | Iterable[GraphDocument],
) -> None:
    """Prints the relationships in the graph documents."""
    if isinstance(graph_documents, GraphDocument):
        graph_documents = [graph_documents]

    for doc in graph_documents:
        for relation in doc.relationships:
            source = relation.source
            target = relation.target
            print(f"{_node_label(source)} -> {_node_label(target)}: {relation.type}")  # noqa: T201


def render_graph_documents(
    graph_documents: GraphDocument | Iterable[GraphDocument],
) -> graphviz.Digraph:
    """Renders the relationships in the graph documents."""
    if isinstance(graph_documents, GraphDocument):
        graph_documents = [graph_documents]

    dot = graphviz.Digraph()

    nodes: dict[tuple[str | int, str], str] = {}

    def _node_id(node: Node) -> str:
        node_key = (node.id, node.type)
        if node_id := nodes.get(node_key):
            return node_id
        node_id = f"{len(nodes)}"
        nodes[node_key] = node_id
        dot.node(node_id, label=_node_label(node))
        return node_id

    for graph_document in graph_documents:
        for node in graph_document.nodes:
            _node_id(node)
        for r in graph_document.relationships:
            dot.edge(_node_id(r.source), _node_id(r.target), r.type)

    return dot


def render_knowledge_schema(knowledge_schema: KnowledgeSchema) -> graphviz.Digraph:
    """Renders the knowledge schema as a graph."""
    dot = graphviz.Digraph()

    for node in knowledge_schema.nodes:
        dot.node(node.type, tooltip=node.description)

    for r in knowledge_schema.relationships:
        for source in r.source_types:
            for target in r.target_types:
                dot.edge(source, target, label=r.edge_type, tooltip=r.description)

    return dot
