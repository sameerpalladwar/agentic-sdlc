from langgraph.graph import StateGraph, START, END
from orchestrator.state import WorkflowState
from agents.requirements import requirements_agent


def build_graph():
    """Build and compile the SDLC workflow."""
    graph = StateGraph(WorkflowState)
    graph.add_node("requirements", requirements_agent)
    graph.add_edge(START, "requirements")
    graph.add_edge("requirements", END)
    return graph.compile()
