"""Generate technical design candidates for deterministic validation and bounded recovery."""

import json
import os

from dotenv import load_dotenv
from openai import (
    AuthenticationError,
    BadRequestError,
    OpenAI,
    OpenAIError,
    PermissionDeniedError,
    RateLimitError,
)
from pydantic import ValidationError

from agents.prompt_loader import DESIGN_PROMPT, load_prompt
from agents.requirements import ENV_PATH
from models.design import DesignOutput
from models.design_validation import issue
from models.requirements import RequirementsOutput
from orchestrator.state import WorkflowState


def design_agent(state: WorkflowState) -> dict:
    """Generate a candidate. A separate node validates every attempt before routing."""
    base = {
        "design": None,
        "design_candidate": None,
        "design_generation_issues": [],
        "status": "design_generated",
        "error": None,
    }

    def failure(code: str, field: str, message: str, retryable: bool = False) -> dict:
        return {**base, "design_generation_issues": [issue(code, field, message, retryable)]}

    try:
        requirements = RequirementsOutput.model_validate(state.get("requirements"))
    except (ValidationError, TypeError, ValueError):
        return failure(
            "INVALID_REQUIREMENTS", "requirements", "Design requires valid structured requirements."
        )
    if (
        state.get("status") not in {"requirements_complete", "design_retrying"}
        or requirements.requires_human_clarification
    ):
        return failure(
            "UNAPPROVED_REQUIREMENTS",
            "requirements",
            "Design requires completed requirements without blocking clarification.",
        )
    try:
        # SDK JSON schema generation errors are programming errors, not bad model output.
        DesignOutput.model_json_schema()
    except Exception:
        return failure(
            "SCHEMA_CONTRACT",
            "DesignOutput",
            "Repair the DesignOutput JSON schema contract; automatic correction cannot fix SDK programming errors.",
        )
    try:
        load_dotenv(ENV_PATH)
    except OSError:
        return failure(
            "CONFIG_PERMISSION",
            "environment",
            "Unable to read environment configuration; check file permissions.",
        )
    api_key, model = os.getenv("OPENAI_API_KEY", "").strip(), os.getenv("OPENAI_MODEL", "").strip()
    if not api_key or not model:
        return failure(
            "CONFIGURATION",
            "environment",
            "Set OPENAI_API_KEY and OPENAI_MODEL in .env or the environment.",
        )
    try:
        prompt = load_prompt(DESIGN_PROMPT)
    except (OSError, ValueError):
        return failure(
            "PROMPT_CONFIGURATION", "prompts/design.md", "Unable to load the design prompt."
        )
    context = {
        "requirements": requirements.model_dump(mode="json"),
        "original_requirement": state["requirement"],
        "clarifications": state.get("clarifications", []),
    }
    if state.get("design_retry_count", 0):
        from orchestrator.design_validation import redact

        context["correction"] = {
            "current_invalid_design": redact(state.get("design_candidate")),
            "validation_issues": state["design_validation_issues"],
            "instructions": "Fix only the reported defects. Preserve valid design elements and approved story IDs. Return the full corrected DesignOutput.",
        }
    try:
        with OpenAI(api_key=api_key, timeout=60.0, max_retries=0) as client:
            response = client.responses.parse(
                model=model,
                input=[
                    {"role": "system", "content": prompt},
                    {"role": "user", "content": json.dumps(context, ensure_ascii=False)},
                ],
                text_format=DesignOutput,
            )
        if any(
            getattr(c, "type", None) == "refusal"
            for item in getattr(response, "output", [])
            for c in getattr(item, "content", [])
        ):
            return failure(
                "REFUSAL",
                "response",
                "The model refused the request; review the requirement before retrying.",
            )
        if response.status != "completed" or response.output_parsed is None:
            return failure(
                "INCOMPLETE_OUTPUT",
                "response",
                "Return a complete structured design with all required fields.",
                True,
            )
        candidate = response.output_parsed
        if isinstance(candidate, DesignOutput):
            candidate = candidate.model_dump(mode="json")
        if not isinstance(candidate, dict):
            return failure(
                "INVALID_OUTPUT", "response", "Return a JSON object matching DesignOutput.", True
            )
        from orchestrator.design_validation import redact

        sanitized = redact(candidate)
        if sanitized != candidate:
            return {
                **base,
                "design_candidate": sanitized,
                "design_generation_issues": [
                    issue(
                        "SECURITY_SECRET",
                        "design",
                        "Sensitive credential-like content was removed. Review input/security before a new run.",
                        False,
                    )
                ],
            }
        return {**base, "design_candidate": sanitized}
    except ValidationError as exc:
        issues = [
            issue("SCHEMA_INVALID", ".".join(map(str, e["loc"])) or "design", e["msg"])
            for e in exc.errors(include_input=False, include_context=False, include_url=False)
        ]
        from orchestrator.design_validation import redact

        return {**base, "design_generation_issues": redact(issues)}
    except (AuthenticationError, PermissionDeniedError):
        return failure(
            "API_PERMISSION",
            "openai",
            "Check API credentials and model/project permissions; correction retries are disabled.",
        )
    except BadRequestError:
        return failure(
            "API_CONTRACT",
            "openai",
            "Check model support and structured-output schema configuration.",
        )
    except RateLimitError:
        return failure("API_QUOTA", "openai", "Check API quota or rate limits before a new run.")
    except OpenAIError:
        return failure(
            "API_ERROR",
            "openai",
            "OpenAI design request failed. Check connectivity and API configuration.",
        )
    except ValueError:
        return failure(
            "INVALID_OUTPUT", "response", "Return valid complete JSON matching DesignOutput.", True
        )
    except TypeError:
        return failure(
            "SCHEMA_CONTRACT",
            "openai",
            "Check SDK arguments and schema types; this contract error is not retryable.",
        )
