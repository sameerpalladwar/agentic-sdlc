"""Deterministic checks and bounded correction routing; never execute model output."""

import json
import os
import re
from collections import Counter

from pydantic import ValidationError

from models.design import DesignOutput
from models.design_validation import issue
from models.requirements import RequirementsOutput
from orchestrator.state import WorkflowState

MAX_CORRECTION_RETRIES = 2


def redact(value):
    text = json.dumps(value, ensure_ascii=False)
    for name in ("OPENAI_API_KEY", "AWS_SECRET_ACCESS_KEY", "AZURE_OPENAI_API_KEY"):
        secret = os.getenv(name, "")
        if secret:
            text = text.replace(secret, "[REDACTED]")
    text = re.sub(r"sk-[A-Za-z0-9_-]{12,}", "[REDACTED]", text)
    return json.loads(text)


def validate_design(
    candidate: object, requirements: RequirementsOutput, request: str = ""
) -> tuple[dict | None, list[dict]]:
    """Validate schema, coverage and design references against approved stories; return
    actionable issues.
    """
    issues = []
    try:
        design = DesignOutput.model_validate(candidate)
    except ValidationError as exc:
        for error in exc.errors(include_input=False, include_context=False, include_url=False):
            field = ".".join(map(str, error["loc"])) or "design"
            issues.append(issue("SCHEMA_INVALID", field, error["msg"]))
        return None, issues
    expected = {story.id: story for story in requirements.user_stories}
    counts = Counter(entry.story_id for entry in design.story_traceability)
    for story_id in expected:
        if story_id not in counts:
            issues.append(
                issue(
                    "MISSING_STORY",
                    "story_traceability",
                    "Add exactly one traceability entry for " + story_id + ".",
                    stories=[story_id],
                )
            )
    for index, entry in enumerate(design.story_traceability):
        field = f"story_traceability.{index}"
        if entry.story_id not in expected:
            issues.append(
                issue(
                    "UNKNOWN_STORY",
                    field + ".story_id",
                    "Reference only story IDs from the approved requirements.",
                    stories=[entry.story_id],
                )
            )
        elif len(entry.acceptance_criteria_coverage) != len(
            expected[entry.story_id].acceptance_criteria
        ):
            count = len(expected[entry.story_id].acceptance_criteria)
            issues.append(
                issue(
                    "CRITERION_COVERAGE",
                    field + ".acceptance_criteria_coverage",
                    f"Provide exactly {count} coverage explanations in criterion order.",
                    stories=[entry.story_id],
                )
            )
        if counts[entry.story_id] > 1:
            issues.append(
                issue(
                    "DUPLICATE_STORY",
                    field + ".story_id",
                    "Keep exactly one traceability entry for this story.",
                    stories=[entry.story_id],
                )
            )
    # Canonical references are strict; legacy descriptive references remain supported
    # only if they contain a declared name as a complete token sequence.
    refs = {
        "component": {c.name for c in design.components},
        "endpoint": {f"{e.method} {e.path}" for e in design.api_endpoints},
        "data_model": {m.name for m in design.data_models},
    }
    for kind, names in refs.items():
        items = (
            design.components
            if kind == "component"
            else (design.api_endpoints if kind == "endpoint" else design.data_models)
        )
        if len(names) != len(items):
            issues.append(issue("DUPLICATE_ELEMENT", kind, "Design element names must be unique."))
    for index, entry in enumerate(design.story_traceability):
        for ref_index, ref in enumerate(entry.design_elements):
            kind, separator, name = ref.partition(":")
            if separator and kind in refs:
                valid = name.strip() in refs[kind]
            else:
                names = set().union(*refs.values())
                # Accept existing 'POST /urls endpoint' and 'URL API POST /urls' forms.
                remaining = ref
                matched = False
                for declared in sorted(names, key=len, reverse=True):
                    remaining, count = re.subn(
                        r"(?<![\w])" + re.escape(declared) + r"(?![\w])", "", remaining
                    )
                    matched |= count > 0
                remaining = re.sub(
                    r"\b(?:endpoint|component|data model)\b", "", remaining, flags=re.IGNORECASE
                )
                valid = matched and not remaining.strip(" ,;:/()-")
            if not valid:
                issues.append(
                    issue(
                        "INVALID_DESIGN_REFERENCE",
                        f"story_traceability.{index}.design_elements.{ref_index}",
                        "Use a declared component:<name>, endpoint:<METHOD path>, or data_model:<name> reference.",
                        stories=[entry.story_id],
                    )
                )
    known_dependencies = refs["component"] | {t.name for t in design.technology_stack}
    for index, component in enumerate(design.components):
        for dependency in component.dependencies:
            if dependency not in known_dependencies:
                issues.append(
                    issue(
                        "INVALID_COMPONENT_REFERENCE",
                        f"components.{index}.dependencies",
                        "Reference only declared component or technology names in dependencies.",
                    )
                )
        if any(not text.strip() for text in component.responsibilities):
            issues.append(
                issue(
                    "EMPTY_RESPONSIBILITY",
                    f"components.{index}.responsibilities",
                    "Provide nonempty component responsibilities.",
                )
            )
    for index, entry in enumerate(design.story_traceability):
        if any(not text.strip() for text in entry.acceptance_criteria_coverage):
            issues.append(
                issue(
                    "EMPTY_CRITERION_COVERAGE",
                    f"story_traceability.{index}.acceptance_criteria_coverage",
                    "Explain how each criterion is supported; blank coverage is not valid.",
                    stories=[entry.story_id],
                )
            )
    if any(not step.strip() for step in design.implementation_plan):
        issues.append(
            issue(
                "EMPTY_IMPLEMENTATION_STEP",
                "implementation_plan",
                "Provide actionable nonempty implementation steps.",
            )
        )
    endpoints = {(e.method, e.path) for e in design.api_endpoints}
    for method, path in re.findall(
        r"\b(GET|POST|PUT|PATCH|DELETE|HEAD|OPTIONS)\s+(/[A-Za-z0-9_{}./-]+)", request
    ):
        path = path.rstrip(".,")
        if (method, path) not in endpoints:
            issues.append(
                issue(
                    "REQUIRED_ENDPOINT",
                    "api_endpoints",
                    f"Include explicitly requested endpoint {method} {path}.",
                )
            )
    # Enforce explicit positive technology choices; do not guess architecture from adjectives.
    requested = []
    for match in re.finditer(r"\b(?:using|use|with)\s+([^.!?\n]+)", request, re.IGNORECASE):
        phrase = match.group(1)
        for name in ("FastAPI", "SQLite", "PostgreSQL", "Django", "Flask"):
            if re.search(r"\b" + name + r"\b", phrase, re.IGNORECASE) and not re.search(
                r"\b(?:not|no|without|avoid|instead of)\s+" + name, phrase, re.IGNORECASE
            ):
                requested.append(name)
    for name in set(requested):
        if not any(
            re.search(r"\b" + name + r"\b", t.name, re.IGNORECASE) for t in design.technology_stack
        ):
            issues.append(
                issue(
                    "REQUIRED_TECHNOLOGY",
                    "technology_stack",
                    "Preserve explicitly requested technology " + name + ".",
                )
            )
    return design.model_dump(mode="json"), issues


def design_validator(state: WorkflowState) -> dict:
    """Record candidate validation in shared state/history before routing to planning or
    correction.
    """
    issues = list(state.get("design_generation_issues", []))
    validated = None
    if not issues:
        try:
            requirements = RequirementsOutput.model_validate(state.get("requirements"))
            if requirements.requires_human_clarification:
                issues = [
                    issue(
                        "UNAPPROVED_REQUIREMENTS",
                        "requirements",
                        "Resolve human clarification before design.",
                        False,
                    )
                ]
            else:
                request = (
                    state["requirement"]
                    + "\n"
                    + "\n".join(c["answers"] for c in state.get("clarifications", []))
                )
                validated, issues = validate_design(
                    state.get("design_candidate"), requirements, request
                )
        except (ValidationError, TypeError, KeyError):
            issues = [
                issue(
                    "INVALID_REQUIREMENTS",
                    "requirements",
                    "Repair the structured requirements contract before generating design.",
                    False,
                )
            ]
    valid = not issues
    retry_count = state.get("design_retry_count", 0)
    history = [
        *state.get("recovery_history", []),
        redact(
            {
                "attempt": retry_count + 1,
                "retry_count": retry_count,
                "candidate": state.get("design_candidate"),
                "validation_status": "valid" if valid else "invalid",
                "issues": issues,
            }
        ),
    ]
    return {
        "design": validated if valid else None,
        "status": "design_complete" if valid else "design_invalid",
        "design_validation_status": "valid" if valid else "invalid",
        "design_validation_issues": issues,
        "recovery_history": history,
        "error": None if valid else "Design validation requires correction.",
        "failure_reason": None,
        "design_retry_count": retry_count,
        "design_max_retries": min(max(state.get("design_max_retries", 2), 0), 2),
    }


def route_validation(state: WorkflowState) -> str:
    if state["design_validation_status"] == "valid":
        return "valid"
    count, maximum = (
        state.get("design_retry_count", 0),
        state.get("design_max_retries", MAX_CORRECTION_RETRIES),
    )
    if (
        0 <= count < min(maximum, MAX_CORRECTION_RETRIES)
        and state["design_validation_issues"]
        and all(i["retryable"] for i in state["design_validation_issues"])
    ):
        return "design_retry"
    return "design_safe_stop"


def design_retry(state: WorkflowState) -> dict:
    # Separate checkpoint before the next network request.
    return {
        "design_retry_count": state.get("design_retry_count", 0) + 1,
        "status": "design_retrying",
    }


def design_safe_stop(state: WorkflowState) -> dict:
    """Stop exhausted or nonretryable design generation and expose safe, actionable
    diagnostics.
    """
    issues = state.get("design_validation_issues", [])
    exhausted = all(i["retryable"] for i in issues)
    reason = (
        "Design correction budget exhausted after the initial attempt and at most two retries."
        if exhausted
        else "Design stopped for a non-retryable input, configuration, API, or contract error."
    )
    details = " ".join(i["message"] for i in issues[:5])
    return {
        "status": "failed",
        "design": None,
        "failure_reason": reason,
        "error": reason
        + " "
        + details
        + " Review design_attempts and correct the request or configuration before starting a new run.",
    }
