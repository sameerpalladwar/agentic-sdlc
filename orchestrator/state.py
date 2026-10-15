from typing import Literal, NotRequired, TypedDict


class WorkflowState(TypedDict):
    requirement: str
    requirements: NotRequired[dict | None]
    status: Literal["pending", "requirements_complete", "needs_human_clarification", "failed", "paused", "rejected", "design_complete"]
    error: NotRequired[str | None]
    clarifications: NotRequired[list[dict]]
    through: NotRequired[Literal["requirements", "design"]]
    design: NotRequired[dict | None]
