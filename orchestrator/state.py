from typing import Literal, NotRequired, TypedDict


class WorkflowState(TypedDict):
    requirement: str
    requirements: NotRequired[dict | None]
    status: Literal["pending", "requirements_complete", "needs_human_clarification", "failed", "paused", "rejected"]
    error: NotRequired[str | None]
    clarifications: NotRequired[list[dict]]
