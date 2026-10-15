from typing import TypedDict


class WorkflowState(TypedDict):
    requirement: str
    requirements_summary: str
    status: str
