"""Validated user stories, acceptance criteria, and clarification output contracts."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class AcceptanceCriterion(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    given: str = Field(min_length=1)
    when: str = Field(min_length=1)
    then: str = Field(min_length=1)


class UserStory(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    id: str = Field(min_length=1)
    title: str = Field(min_length=1)
    as_a: str = Field(min_length=1)
    i_want: str = Field(min_length=1)
    so_that: str = Field(min_length=1)
    priority: Literal["must", "should", "could", "wont"]
    acceptance_criteria: list[AcceptanceCriterion] = Field(min_length=1)


class RequirementsOutput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    summary: str = Field(min_length=1)
    user_stories: list[UserStory]
    non_functional_requirements: list[str]
    assumptions: list[str]
    open_questions: list[str]
    requires_human_clarification: bool

    @model_validator(mode="after")
    def validate_consistency(self) -> "RequirementsOutput":
        if not self.user_stories and not self.requires_human_clarification:
            raise ValueError("Provide stories or request clarification.")
        if self.requires_human_clarification and not self.open_questions:
            raise ValueError("Clarification requires open questions.")
        ids = [story.id for story in self.user_stories]
        if len(ids) != len(set(ids)):
            raise ValueError("Story IDs must be unique.")
        return self
