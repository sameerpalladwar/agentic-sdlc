"""Actual pytest evidence and explicitly unverified acceptance-criterion results."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class AcceptanceCriterionResult(BaseModel):
    story_id: str
    criterion_index: int
    status: Literal["not_verified"] = "not_verified"
    explanation: str = "Test-to-criterion mapping is not implemented in this MVP."


class QAResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: Literal["qa_passed", "qa_failed", "qa_blocked"]
    command: str | None = None
    exit_code: int | None = None
    total_tests: int = Field(default=0, ge=0)
    passed_tests: int = Field(default=0, ge=0)
    failed_tests: int = Field(default=0, ge=0)
    skipped_tests: int = Field(default=0, ge=0)
    duration_seconds: float = Field(default=0, ge=0)
    error_summary: str = ""
    test_output: str = ""
    acceptance_criteria_results: list[AcceptanceCriterionResult] = Field(default_factory=list)

    revision: str | None = None
    retry_attempt: int = Field(default=0, ge=0)
