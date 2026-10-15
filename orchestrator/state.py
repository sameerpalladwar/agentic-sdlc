from typing import Literal, NotRequired, TypedDict


class WorkflowState(TypedDict):
    requirement: str
    requirements: NotRequired[dict | None]
    status: Literal["pending", "requirements_complete", "needs_human_clarification", "failed"]
    error: NotRequired[str | None]
