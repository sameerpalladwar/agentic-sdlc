"""Task DAG schema and normalized generated-artifact paths."""

from pathlib import PurePosixPath
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator

NonEmptyText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
TaskStatus = Literal["pending", "ready", "in_progress", "completed", "blocked", "failed"]


def validate_artifact_path(path: str) -> str:
    """Plans may name files only under the approved generated-code directory."""
    parts = PurePosixPath(path).parts
    if (
        "\\" in path
        or ":" in path
        or ".." in parts
        or len(parts) < 3
        or parts[:2] != ("workspace", "output_code")
        or str(PurePosixPath(path)) != path
    ):
        raise ValueError("Artifact paths must be normalized files under workspace/output_code/.")
    return path


class ImplementationTask(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    task_id: NonEmptyText
    title: NonEmptyText
    description: NonEmptyText
    related_user_story_ids: list[NonEmptyText]
    dependencies: list[NonEmptyText]
    expected_artifacts: list[NonEmptyText]
    acceptance_checks: list[NonEmptyText] = Field(min_length=1)
    status: TaskStatus

    @field_validator("expected_artifacts")
    @classmethod
    def validate_artifacts(cls, paths: list[str]) -> list[str]:
        if len(paths) != len(set(paths)):
            raise ValueError("A task must not list the same artifact twice.")
        return [validate_artifact_path(path) for path in paths]


class SharedArtifact(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    path: NonEmptyText
    task_ids: list[NonEmptyText] = Field(min_length=2)
    justification: NonEmptyText

    @field_validator("path")
    @classmethod
    def validate_path(cls, path: str) -> str:
        return validate_artifact_path(path)


class ExecutionPlan(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    tasks: list[ImplementationTask] = Field(min_length=1)
    shared_artifacts: list[SharedArtifact]
    # Derived by Python after validation, never trusted from the LLM.
    ordered_task_ids: list[str] = Field(default_factory=list)
    ready_task_ids: list[str] = Field(default_factory=list)
    blocked_task_ids: list[str] = Field(default_factory=list)
