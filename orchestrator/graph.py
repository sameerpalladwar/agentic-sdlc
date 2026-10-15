from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import StateGraph, START, END
from langgraph.types import interrupt

from orchestrator.qa_recovery import begin_repair, repair_developer
from orchestrator.revision import validation_snapshot
from orchestrator.validation import validation_gate, route_combined, finish_validation
from agents.reviewer import reviewer_agent
from orchestrator.state import WorkflowState
from agents.requirements import requirements_agent
from agents.design import design_agent
from orchestrator.design_validation import design_validator, design_retry, design_safe_stop, route_validation
from agents.planning import planning_agent
from agents.qa import qa_validation_branch
from orchestrator.development import prepare_development, select_task, developer_agent, development_gate


def route_requirements(state: WorkflowState) -> str:
    if state["status"] == "needs_human_clarification":
        return "pause"
    if state["status"] == "requirements_complete" and state.get("through") in {"design", "planning", "development", "qa", "validation"}:
        return "design"
    return END


def route_design(state: WorkflowState) -> str:
    if state["status"] == "design_complete" and state.get("through") in {"planning", "development", "qa", "validation"}:
        return "planning"
    return END


def route_planning(state: WorkflowState) -> str:
    return "development_prepare" if state["status"] == "planning_complete" and state.get("through") in {"development", "qa", "validation"} else END


def route_selection(state: WorkflowState) -> str:
    if state["status"] == "development_paused":
        return "development_gate"
    if state["status"] == "development_complete" and state.get("through") in {"qa", "validation"}:
        return "validation_snapshot"
    return "developer" if state.get("active_task") else END


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
    graph.add_node("validation_snapshot", validation_snapshot)
    graph.add_node("qa", qa_validation_branch)
    graph.add_node("review", reviewer_agent)
    graph.add_node("validation_gate", validation_gate)
    graph.add_node("validation_finish", finish_validation)
    graph.add_node("repair_begin", begin_repair)
    graph.add_node("developer_repair", repair_developer)
    graph.add_conditional_edges("validation_snapshot", lambda s: ["qa", "review"] if s["status"] == "validating" else "validation_finish", ["qa", "review", "validation_finish"])
    graph.add_edge(["qa", "review"], "validation_gate")
    graph.add_conditional_edges("validation_gate", route_combined, ["repair_begin", "validation_finish"])
    graph.add_edge("repair_begin", "developer_repair")
    graph.add_conditional_edges("developer_repair", lambda s: "validation_snapshot" if s.get("qa_repair_applied") else "validation_finish", ["validation_snapshot", "validation_finish"])
    graph.add_edge("validation_finish", END)
    graph.add_node("design_validator", design_validator)
    graph.add_node("design_retry", design_retry)
    graph.add_node("design_safe_stop", design_safe_stop)
    graph.add_edge("design", "design_validator")
    def after_validation(state):
        route = route_validation(state)
        return route_design(state) if route == "valid" else route
    graph.add_conditional_edges("design_validator", after_validation, ["planning", END, "design_retry", "design_safe_stop"])
    graph.add_edge("design_retry", "design")
    graph.add_edge("design_safe_stop", END)
    graph.add_conditional_edges("planning", route_planning, ["development_prepare", END])
    graph.add_node("development_prepare", prepare_development)
    graph.add_node("development_select", select_task)
    graph.add_node("developer", developer_agent)
    graph.add_node("development_gate", development_gate)
    graph.add_conditional_edges("development_prepare", lambda s: "development_select" if s["status"] == "developing" else END, ["development_select", END])
    graph.add_conditional_edges("development_select", route_selection, ["developer", "development_gate", "validation_snapshot", END])
    graph.add_edge("developer", "development_select")
    graph.add_conditional_edges("development_gate", lambda s: "development_select" if s["status"] == "developing" else END, ["development_select", END])
    graph.add_node("pause", mark_paused)
    graph.add_node("clarification", clarify)
    graph.add_edge(START, "requirements")
    graph.add_conditional_edges("requirements", route_requirements, ["pause", "design", END])
    graph.add_edge("pause", "clarification")
    graph.add_conditional_edges("clarification", route_clarification, ["requirements", END])
    return graph.compile(checkpointer=checkpointer)
