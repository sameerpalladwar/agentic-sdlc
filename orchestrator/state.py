"""Shared checkpoint state; parallel validation branches own disjoint result fields."""

from typing import Literal, NotRequired, TypedDict


class WorkflowState(TypedDict):
    requirement: str
    requirements: NotRequired[dict | None]
    status: Literal[
        "pending",
        "requirements_complete",
        "needs_human_clarification",
        "failed",
        "paused",
        "rejected",
        "design_complete",
        "planning_complete",
        "developing",
        "development_paused",
        "development_complete",
        "qa_passed",
        "qa_failed",
        "qa_blocked",
        "qa_error",
        "design_generated",
        "design_invalid",
        "design_retrying",
        "repairing",
        "validating",
        "validation_passed",
        "validation_failed",
        "validation_blocked",
        "awaiting_approval",
        "approval_blocked",
        "approved",
        "human_changes_requested",
        "completed",
    ]
    error: NotRequired[str | None]
    clarifications: NotRequired[list[dict]]
    through: NotRequired[
        Literal["requirements", "design", "planning", "development", "qa", "validation", "delivery"]
    ]
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

    validation_revision: NotRequired[str | None]
    validation_files: NotRequired[dict[str, str]]
    qa_result: NotRequired[dict | None]
    review_result: NotRequired[dict | None]
    validation_result: NotRequired[dict | None]

    approval_request: NotRequired[dict | None]
    human_approval: NotRequired[dict | None]
    approval_history: NotRequired[list[dict]]
    human_change_count: NotRequired[int]
    final_manifest: NotRequired[dict | None]
    run_directory: NotRequired[str]
