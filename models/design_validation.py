from pydantic import BaseModel, ConfigDict


class DesignValidationIssue(BaseModel):
    model_config = ConfigDict(extra="forbid")
    code: str
    field: str
    message: str
    retryable: bool
    affected_story_ids: list[str]


def issue(code: str, field: str, message: str, retryable: bool = True,
          stories: list[str] | None = None) -> dict:
    return DesignValidationIssue(code=code, field=field, message=message,
                                 retryable=retryable, affected_story_ids=stories or []).model_dump()
