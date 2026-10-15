"""Structured file changes and statically claimed implementation coverage."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from models.planning import NonEmptyText


class FileChange(BaseModel):
    model_config = ConfigDict(extra="forbid")
    path: NonEmptyText
    content: str


class AcceptanceCheckMapping(BaseModel):
    model_config = ConfigDict(extra="forbid")
    check: NonEmptyText
    implementation: NonEmptyText


class DeveloperOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    task_id: NonEmptyText
    files: list[FileChange]
    implementation_summary: NonEmptyText
    acceptance_check_mapping: list[AcceptanceCheckMapping] = Field(min_length=1)
    assumptions: list[str]
    warnings: list[str]
    completion_status: Literal["completed", "blocked", "failed"]
