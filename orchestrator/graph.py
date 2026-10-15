from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import StateGraph, START, END
from langgraph.types import interrupt

from orchestrator.state import WorkflowState
from agents.requirements import requirements_agent
from agents.design import design_agent
from agents.planning import planning_agent


def route_requirements(state: WorkflowState) -> str:
    if state["status"] == "needs_human_clarification":
        return "pause"
    if state["status"] == "requirements_complete" and state.get("through") in {"design", "planning"}:
        return "design"
    return END


def route_design(state: WorkflowState) -> str:
    if state["status"] == "design_complete" and state.get("through") == "planning":
        return "planning"
    return END


def mark_paused(state: WorkflowState) -> dict:
    return {"status": "paused"}


def clarify(state: WorkflowState) -> dict:
    """No side effects before interrupt: LangGraph replays this node on resume."""
    decision = interrupt({"open_questions": state["requirements"]["open_questions"],
                          "instructions": "Answer the questions or reject this run."})
    if not isinstance(decision, dict):
        raise ValueError("Resume requires answers or rejection.")
    if decision.get("reject") is True:
        return {"status": "rejected", "error": None}
    answers = decision.get("answers")
    if not isinstance(answers, str) or not answers.strip():
        raise ValueError("Answers must be nonempty text.")
    history = [*state.get("clarifications", []), {
        "questions": state["requirements"]["open_questions"], "answers": answers.strip(),
    }]
    return {"clarifications": history, "status": "pending", "error": None}


def route_clarification(state: WorkflowState) -> str:
    return END if state["status"] == "rejected" else "requirements"


def build_graph(checkpointer: BaseCheckpointSaver | None = None):
    """Build the requirements workflow; callers supply durable checkpointing."""
    graph = StateGraph(WorkflowState)
    graph.add_node("requirements", requirements_agent)
    graph.add_node("design", design_agent)
    graph.add_node("planning", planning_agent)
    graph.add_conditional_edges("design", route_design, ["planning", END])
    graph.add_edge("planning", END)
    graph.add_node("pause", mark_paused)
    graph.add_node("clarification", clarify)
    graph.add_edge(START, "requirements")
    graph.add_conditional_edges("requirements", route_requirements, ["pause", "design", END])
    graph.add_edge("pause", "clarification")
    graph.add_conditional_edges("clarification", route_clarification, ["requirements", END])
    return graph.compile(checkpointer=checkpointer)
