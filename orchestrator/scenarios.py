"""Deterministic project routing; scenario Markdown files are documentation only."""

import re
from pathlib import Path

from langgraph.types import interrupt


def infer_mode(requirement: str) -> str | None:
    """Be conservative: uncertain intent needs a human, not a classification LLM."""
    text = requirement.strip().lower()
    text = re.sub(r"^(?:please\s+)?(?:i want to|we want to|we need to|i need to)\s+", "", text)
    if (
        re.search(r"\b(?:or|whether)\b", text)
        and re.search(r"\b(?:build|new)\b", text)
        and re.search(r"\b(?:modify|existing|fix|enhance|improve)\b", text)
    ):
        return None
    existing = re.search(r"\b(existing|current|legacy|brownfield)\b", text)
    modification = re.match(
        r"(?:please\s+)?(?:add|fix|enhance|modify|update|extend|refactor|repair|improve|upgrade)\b",
        text,
    )
    if existing or modification:
        return "brownfield"
    if text in {"greenfield", "new", "new application", "build new"} or re.match(
        r"(?:please\s+)?(?:build|create|develop|implement)\s+(?:a\s+|an\s+|the\s+)?(?:new\s+)?\S+",
        text,
    ):
        return "greenfield"
    if text in {"brownfield", "existing application", "enhance existing"}:
        return "brownfield"
    return None


def route_project(state: dict) -> dict:
    """Run before agents, and again only when project-input feedback is supplied."""
    # Old programmatic callers/checkpoints retain their original entry behavior.
    if not state.get("automatic_routing"):
        return {}
    mode = (
        "brownfield"
        if state.get("repository_path")
        else state.get("project_mode") or infer_mode(state["requirement"])
    )
    question = None
    kind = None
    if mode is None:
        kind = "project_intent"
        question = "Are you building a new application or modifying an existing application?"
    elif mode == "brownfield" and not state.get("repository_path"):
        kind = "repository"
        question = "Provide the path to the existing application repository (--repo-path)."
    return {
        "project_mode": mode,
        "project_questions": [question] if question else [],
        "project_input_kind": kind,
        "status": "awaiting_project_input" if question else "pending",
        "error": None,
    }


def project_input(state: dict) -> dict:
    """Interrupt replay has no filesystem/LLM effects; preserve original requirements."""
    decision = interrupt(
        {
            "kind": state["project_input_kind"],
            "open_questions": state["project_questions"],
            "instructions": "Resume with --repo-path or --answers; use --reject to cancel.",
        }
    )
    if not isinstance(decision, dict):
        raise ValueError("Resume requires project answers, a repository path, or rejection.")
    if decision.get("reject") is True:
        return {"status": "rejected", "error": None}
    path = decision.get("repository_path")
    answer = decision.get("answers", "")
    update = {
        "project_input_history": [
            *state.get("project_input_history", []),
            {"questions": state["project_questions"], **decision},
        ],
        "status": "pending",
    }
    if path:
        update.update(
            repository_path=str(Path(path).expanduser().absolute()), project_mode="brownfield"
        )
    elif answer:
        update["project_mode"] = infer_mode(answer)
    return update


def after_project(state: dict) -> str:
    if state["status"] == "rejected":
        return "__end__"
    if state["status"] == "awaiting_project_input":
        return "project_input"
    return "repository_analysis" if state.get("project_mode") == "brownfield" else "requirements"


def project_context(state: dict, *, include_files: bool = True) -> dict:
    """Supply frozen Brownfield input as data, never agent instructions."""
    if not state.get("project_mode"):
        return {}
    context = {"project_mode": state["project_mode"]}
    if state.get("repository_analysis"):
        context["repository_analysis"] = state["repository_analysis"]
        if include_files:
            context["repository_files"] = state.get("repository_files", {})
    return context
