from typing import Literal, NotRequired, TypedDict


class WorkflowState(TypedDict):
    requirement: str
    requirements: NotRequired[dict | None]
    status: Literal["pending", "requirements_complete", "needs_human_clarification", "failed", "paused", "rejected", "design_complete", "planning_complete", "developing", "development_paused", "development_complete", "qa_passed", "qa_failed", "qa_blocked", "qa_error", "design_generated", "design_invalid", "design_retrying", "repairing"]
    error: NotRequired[str | None]
    clarifications: NotRequired[list[dict]]
    through: NotRequired[Literal["requirements", "design", "planning", "development", "qa"]]
    design: NotRequired[dict | None]
    plan: NotRequired[dict | None]
    run_id: NotRequired[str]
    development_budget: NotRequired[int]
    development_used: NotRequired[int]
    active_task: NotRequired[str | None]
    development_issues: NotRequired[list[dict]]
    development_results: NotRequired[list[dict]]
    qa_results: NotRequired[dict | None]
    design_candidate: NotRequired[dict | None]
    design_generation_issues: NotRequired[list[dict]]
    design_validation_status: NotRequired[Literal["valid", "invalid"] | None]
    design_validation_issues: NotRequired[list[dict]]
    design_retry_count: NotRequired[int]
    design_max_retries: NotRequired[int]
    recovery_history: NotRequired[list[dict]]
    failure_reason: NotRequired[str | None]
    qa_local_authorized: NotRequired[bool]
    application_dir: NotRequired[str]

    qa_feedback: NotRequired[dict]
    qa_repair_count: NotRequired[int]
    qa_repair_applied: NotRequired[bool]
