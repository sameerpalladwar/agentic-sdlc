from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class ReviewFinding(BaseModel):
    model_config = ConfigDict(extra='forbid')
    finding_id: str = Field(min_length=1)
    severity: Literal['critical', 'high', 'medium', 'low', 'informational']
    category: str = Field(min_length=1)
    file_path: str
    line_number: int | None = Field(default=None, ge=1)
    description: str = Field(min_length=1)
    code_evidence: str = Field(min_length=1)
    recommended_fix: str = Field(min_length=1)
    related_user_story_ids: list[str]


class CodeReviewResult(BaseModel):
    model_config = ConfigDict(extra='forbid')
    status: Literal['approved', 'changes_requested', 'blocked', 'error']
    summary: str
    findings: list[ReviewFinding]
    blocking_findings_count: int = Field(ge=0)
    reviewed_files: list[str]
    reviewed_revision: str
    retry_attempt: int = Field(default=0, ge=0)
