from typing import Literal, NotRequired, TypedDict


class WorkflowState(TypedDict):
    requirement: str
    requirements: NotRequired[dict | None]
    status: Literal["pending", "requirements_complete", "needs_human_clarification", "failed", "paused", "rejected", "design_complete", "planning_complete", "developing", "development_paused", "development_complete"]
    error: NotRequired[str | None]
    clarifications: NotRequired[list[dict]]
    through: NotRequired[Literal["requirements", "design", "planning", "development"]]
    design: NotRequired[dict | None]
    plan: NotRequired[dict | None]
    run_id: NotRequired[str]
    development_budget: NotRequired[int]
    development_used: NotRequired[int]
    active_task: NotRequired[str | None]
    development_results: NotRequired[list[dict]]
