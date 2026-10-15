"""Translate approved requirements and design into a validated execution DAG."""

import json
import os

from dotenv import load_dotenv
from openai import OpenAI, OpenAIError
from pydantic import ValidationError

from agents.prompt_loader import PLANNING_PROMPT, load_prompt
from agents.requirements import ENV_PATH
from models.design import DesignOutput
from models.planning import ExecutionPlan
from models.requirements import RequirementsOutput
from orchestrator.design_validation import redact
from orchestrator.scenarios import project_context
from orchestrator.state import WorkflowState
from orchestrator.task_plan import ArtifactOwnershipError, validate_execution_plan


def planning_agent(state: WorkflowState) -> dict:
    """Plan from existing validated design and requirements; never execute tasks."""
    failure = {"plan": None, "status": "failed", "error": None}
    try:
        requirements = RequirementsOutput.model_validate(state.get("requirements"))
        design = DesignOutput.model_validate(state.get("design"))
    except (ValidationError, ValueError, TypeError):
        return {**failure, "error": "Planning requires valid structured requirements and design."}
    if state.get("status") != "design_complete" or requirements.requires_human_clarification:
        return {
            **failure,
            "error": "Planning requires completed design without blocking clarification.",
        }

    load_dotenv(ENV_PATH)
    api_key = os.getenv("OPENAI_API_KEY", "").strip()
    model = os.getenv("OPENAI_MODEL", "").strip()
    if not api_key or not model:
        return {
            **failure,
            "error": "Set OPENAI_API_KEY and OPENAI_MODEL in .env or the environment.",
        }
    try:
        prompt = load_prompt(PLANNING_PROMPT)
    except (OSError, ValueError):
        return {**failure, "error": "Unable to load the planning prompt."}
    context = {
        "requirements": requirements.model_dump(mode="json"),
        "design": design.model_dump(mode="json"),
    }
    context.update(project_context(state, include_files=False))
    if state.get("planning_retry_count", 0):
        context["correction"] = {
            "invalid_plan": state.get("planning_candidate"),
            "validation_issues": state.get("planning_validation_issues", []),
            "instructions": "Correct only the ownership defects; preserve valid design, story coverage and task dependencies. Return a complete plan; do not invent justifications or remove required tasks to evade validation.",
        }
    try:
        with OpenAI(api_key=api_key, timeout=60.0, max_retries=2) as client:
            response = client.responses.parse(
                model=model,
                input=[
                    {"role": "system", "content": prompt},
                    {"role": "user", "content": json.dumps(context, ensure_ascii=False)},
                ],
                text_format=ExecutionPlan,
            )
        if response.status != "completed" or response.output_parsed is None:
            return {
                **failure,
                "error": "The model returned no complete structured plan (possibly a refusal).",
            }
        plan = ExecutionPlan.model_validate(response.output_parsed)
    except ValidationError:
        return {**failure, "error": "The model returned an invalid plan schema."}
    except ValueError:
        return {**failure, "error": "The model returned an invalid plan."}
    except TypeError:
        return {**failure, "error": "The model returned an invalid plan."}
    except OpenAIError:
        return {
            **failure,
            "error": "OpenAI planning request failed. Check credentials, model access, and connectivity.",
        }
    try:
        plan = validate_execution_plan(plan, requirements)
    except ArtifactOwnershipError as exc:
        count = state.get("planning_retry_count", 0)
        issue = {
            "code": "invalid_artifact_ownership",
            "field": "shared_artifacts",
            "message": str(exc),
        }
        attempt = redact(
            {
                "attempt": count + 1,
                "candidate": plan.model_dump(mode="json"),
                "issues": [issue],
                "outcome": "invalid",
            }
        )
        return {
            **failure,
            "status": "planning_invalid" if count < 2 else "failed",
            "error": ("Planning correction budget exhausted. " if count >= 2 else "") + str(exc),
            "planning_candidate": attempt["candidate"],
            "planning_validation_issues": [issue],
            "planning_attempts": [*state.get("planning_attempts", []), attempt],
        }
    except ValueError as exc:
        return {**failure, "error": "Plan validation failed: " + str(exc)}
    return {
        "plan": plan.model_dump(mode="json"),
        "status": "planning_complete",
        "error": None,
        "planning_candidate": None,
        "planning_validation_issues": [],
        "planning_attempts": [
            *state.get("planning_attempts", []),
            {"attempt": state.get("planning_retry_count", 0) + 1, "outcome": "valid", "issues": []},
        ],
    }
