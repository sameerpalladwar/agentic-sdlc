"""Structured technical design and story-traceability contracts."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class DesignModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class Technology(DesignModel):
    name: str = Field(min_length=1)
    purpose: str = Field(min_length=1)
    rationale: str = Field(min_length=1)


class Component(DesignModel):
    name: str = Field(min_length=1)
    responsibilities: list[str] = Field(min_length=1)
    dependencies: list[str]


class APIEndpoint(DesignModel):
    method: Literal["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"]
    path: str = Field(pattern=r"^/")
    request: str = Field(min_length=1)
    response: str = Field(min_length=1)
    errors: list[str]


class DataField(DesignModel):
    name: str = Field(min_length=1)
    type: str = Field(min_length=1)
    constraints: str = Field(min_length=1)


class DataModel(DesignModel):
    name: str = Field(min_length=1)
    fields: list[DataField] = Field(min_length=1)
    relationships: list[str]


class StoryTraceability(DesignModel):
    story_id: str = Field(min_length=1)
    design_elements: list[str] = Field(min_length=1)
    acceptance_criteria_coverage: list[str] = Field(min_length=1)


class DesignOutput(DesignModel):
    architecture_summary: str = Field(min_length=1)
    technology_stack: list[Technology] = Field(min_length=1)
    components: list[Component] = Field(min_length=1)
    api_endpoints: list[APIEndpoint]
    data_models: list[DataModel]
    non_functional_design: list[str]
    implementation_plan: list[str] = Field(min_length=1)
    assumptions: list[str]
    risks: list[str]
    story_traceability: list[StoryTraceability] = Field(min_length=1)
