"""Workflow routing, durable human interrupts, and parallel validation barriers."""

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from agents.design import design_agent
from agents.planning import planning_agent
from agents.qa import qa_validation_branch
from agents.repository_analysis import repository_analysis_agent
from agents.requirements import requirements_agent
from agents.reviewer import reviewer_agent
from orchestrator.approval import approval_prepare, final_delivery, human_approval, human_repair
from orchestrator.design_validation import (
    design_retry,
    design_safe_stop,
    design_validator,
    route_validation,
)
from orchestrator.development import (
    developer_agent,
    development_gate,
    prepare_development,
    select_task,
)
from orchestrator.observability import observe_node
from orchestrator.qa_recovery import begin_repair, repair_developer
from orchestrator.revision import validation_snapshot
from orchestrator.scenarios import after_project, project_input, route_project
from orchestrator.state import WorkflowState
from orchestrator.validation import finish_validation, route_combined, validation_gate


def route_requirements(state: WorkflowState) -> str:
    if state["status"] == "needs_human_clarification":
        return "pause"
    if state["status"] == "requirements_complete" and state.get("through") in {
        "design",
        "planning",
        "development",
        "qa",
        "validation",
        "delivery",
    }:
        return "design"
    return END


def route_design(state: WorkflowState) -> str:
    if state["status"] == "design_complete" and state.get("through") in {
        "planning",
        "development",
        "qa",
        "validation",
        "delivery",
    }:
        return "planning"
    return END


def route_planning(state: WorkflowState) -> str:
    if state["status"] == "planning_invalid" and state.get("planning_retry_count", 0) < 2:
        return "planning_retry"
    return (
        "development_prepare"
        if state["status"] == "planning_complete"
        and state.get("through") in {"development", "qa", "validation", "delivery"}
        else END
    )


def retry_planning(state: WorkflowState) -> dict:
    """Checkpoint the retry budget before replaying Planning with exact ownership feedback."""
    return {
        "status": "design_complete",
        "planning_retry_count": state.get("planning_retry_count", 0) + 1,
    }


def route_selection(state: WorkflowState) -> str:
    if state["status"] == "development_paused":
        return "development_gate"
    if state["status"] == "development_complete" and state.get("through") in {
        "qa",
        "validation",
        "delivery",
    }:
        return "validation_snapshot"
    return "developer" if state.get("active_task") else END


def mark_paused(state: WorkflowState) -> dict:
    return {"status": "paused"}


def clarify(state: WorkflowState) -> dict:
    """No side effects before interrupt: LangGraph replays this node on resume."""
    decision = interrupt(
        {
            "open_questions": state["requirements"]["open_questions"],
            "instructions": "Answer the questions or reject this run.",
        }
    )
    if not isinstance(decision, dict):
        raise ValueError("Resume requires answers or rejection.")
    if decision.get("reject") is True:
        return {"status": "rejected", "error": None}
    answers = decision.get("answers")
    if not isinstance(answers, str) or not answers.strip():
        raise ValueError("Answers must be nonempty text.")
    history = [
        *state.get("clarifications", []),
        {
            "questions": state["requirements"]["open_questions"],
            "answers": answers.strip(),
        },
    ]
    return {"clarifications": history, "status": "pending", "error": None}


def route_clarification(state: WorkflowState) -> str:
    return END if state["status"] == "rejected" else "requirements"


def build_graph(checkpointer: BaseCheckpointSaver | None = None):
    """Build the requirements workflow; callers supply durable checkpointing."""
    graph = StateGraph(WorkflowState)
    graph.add_node("requirements", observe_node("requirements", requirements_agent))
    graph.add_node("design", observe_node("design", design_agent))
    graph.add_node("planning", observe_node("planning", planning_agent))
    graph.add_node("validation_snapshot", observe_node("validation_snapshot", validation_snapshot))
    graph.add_node("qa", observe_node("qa", qa_validation_branch))
    graph.add_node("review", observe_node("review", reviewer_agent))
    graph.add_node("validation_gate", observe_node("validation_gate", validation_gate))
    graph.add_node("validation_finish", observe_node("validation_finish", finish_validation))
    graph.add_node("repair_begin", observe_node("repair_begin", begin_repair))
    graph.add_node("developer_repair", observe_node("developer_repair", repair_developer))
    graph.add_conditional_edges(
        "validation_snapshot",
        lambda s: ["qa", "review"] if s["status"] == "validating" else "validation_finish",
        ["qa", "review", "validation_finish"],
    )
    # Explicit fan-in waits for both disjoint branch writes before any repair can start.
    graph.add_edge(["qa", "review"], "validation_gate")
    graph.add_conditional_edges(
        "validation_gate", route_combined, ["repair_begin", "validation_finish"]
    )
    graph.add_edge("repair_begin", "developer_repair")
    graph.add_conditional_edges(
        "developer_repair",
        lambda s: "validation_snapshot" if s.get("qa_repair_applied") else "validation_finish",
        ["validation_snapshot", "validation_finish"],
    )
    graph.add_node("approval_prepare", observe_node("approval_prepare", approval_prepare))
    graph.add_node("human_approval", observe_node("human_approval", human_approval))
    graph.add_node("human_repair", observe_node("human_repair", human_repair))
    graph.add_node("delivery", observe_node("delivery", final_delivery))
    # Validation is still a supported stopping point; delivery adds mandatory human consent.
    graph.add_conditional_edges(
        "validation_finish",
        lambda s: (
            "approval_prepare"
            if s["status"] == "validation_passed" and s.get("through") == "delivery"
            else END
        ),
        ["approval_prepare", END],
    )
    graph.add_conditional_edges(
        "approval_prepare",
        lambda s: "human_approval" if s["status"] == "awaiting_approval" else END,
        ["human_approval", END],
    )
    graph.add_conditional_edges(
        "human_approval",
        lambda s: (
            "delivery"
            if s["status"] == "approved"
            else "human_repair"
            if s["status"] == "human_changes_requested"
            else END
        ),
        ["delivery", "human_repair", END],
    )
    graph.add_conditional_edges(
        "human_repair",
        lambda s: "validation_snapshot" if s.get("qa_repair_applied") else "validation_finish",
        ["validation_snapshot", "validation_finish"],
    )
    graph.add_edge("delivery", END)
    graph.add_node("design_validator", observe_node("design_validator", design_validator))
    graph.add_node("design_retry", observe_node("design_retry", design_retry))
    graph.add_node("design_safe_stop", observe_node("design_safe_stop", design_safe_stop))
    graph.add_edge("design", "design_validator")

    def after_validation(state):
        route = route_validation(state)
        return route_design(state) if route == "valid" else route

    graph.add_conditional_edges(
        "design_validator", after_validation, ["planning", END, "design_retry", "design_safe_stop"]
    )
    graph.add_edge("design_retry", "design")
    graph.add_edge("design_safe_stop", END)
    graph.add_node("planning_retry", observe_node("planning_retry", retry_planning))
    graph.add_conditional_edges(
        "planning", route_planning, ["planning_retry", "development_prepare", END]
    )
    graph.add_edge("planning_retry", "planning")
    graph.add_node("development_prepare", observe_node("development_prepare", prepare_development))
    graph.add_node("development_select", observe_node("development_select", select_task))
    graph.add_node("developer", observe_node("developer", developer_agent))
    graph.add_node("development_gate", observe_node("development_gate", development_gate))
    graph.add_conditional_edges(
        "development_prepare",
        lambda s: "development_select" if s["status"] == "developing" else END,
        ["development_select", END],
    )
    graph.add_conditional_edges(
        "development_select",
        route_selection,
        ["developer", "development_gate", "validation_snapshot", END],
    )
    graph.add_edge("developer", "development_select")
    graph.add_conditional_edges(
        "development_gate",
        lambda s: "development_select" if s["status"] == "developing" else END,
        ["development_select", END],
    )
    graph.add_node("pause", observe_node("pause", mark_paused))
    graph.add_node("clarification", observe_node("clarification", clarify))
    graph.add_node(
        "repository_analysis", observe_node("repository_analysis", repository_analysis_agent)
    )
    graph.add_node("project_routing", observe_node("project_routing", route_project))
    graph.add_node("project_input", observe_node("project_input", project_input))
    graph.add_edge(START, "project_routing")
    graph.add_conditional_edges(
        "project_routing",
        after_project,
        ["project_input", "repository_analysis", "requirements", END],
    )
    graph.add_conditional_edges(
        "project_input",
        lambda s: END if s["status"] == "rejected" else "project_routing",
        [END, "project_routing"],
    )
    graph.add_conditional_edges(
        "repository_analysis",
        lambda s: (
            "project_input"
            if s["status"] == "awaiting_project_input"
            else "requirements"
            if s["status"] == "pending"
            else END
        ),
        ["project_input", "requirements", END],
    )
    graph.add_conditional_edges("requirements", route_requirements, ["pause", "design", END])
    graph.add_edge("pause", "clarification")
    graph.add_conditional_edges("clarification", route_clarification, ["requirements", END])
    return graph.compile(checkpointer=checkpointer)
