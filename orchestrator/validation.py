"""Fan-in of independent QA and review with deterministic revision-aware policy."""

from agents.reviewer import validate_review
from models.qa import QAResult
from models.requirements import RequirementsOutput
from models.review import CodeReviewResult
from orchestrator.design_validation import redact
from orchestrator.qa_recovery import MAX_REPAIRS, assess_qa
from orchestrator.revision import frozen_files, release_revision


def validation_gate(state: dict) -> dict:
    """Join current QA/Review evidence, release the source barrier, and checkpoint a
    deterministic verdict.
    """
    reason = ""
    status = "validation_blocked"
    qa = state.get("qa_result") if isinstance(state.get("qa_result"), dict) else None
    review = state.get("review_result") if isinstance(state.get("review_result"), dict) else None
    try:
        files = frozen_files(state)
        revision = state["validation_revision"]
        attempt = state.get("qa_repair_count", 0)
        if not qa or not review:
            raise ValueError("Both validation results are required.")
        qa_result = QAResult.model_validate(qa)
        review_result = CodeReviewResult.model_validate(review)
        if (
            qa_result.revision != revision
            or review_result.reviewed_revision != revision
            or qa_result.retry_attempt != attempt
            or review_result.retry_attempt != attempt
        ):
            raise ValueError("Validation results belong to another revision or attempt.")
        review_result = validate_review(
            review_result,
            files,
            revision,
            {s.id for s in RequirementsOutput.model_validate(state["requirements"]).user_stories},
        )
        review = review_result.model_dump(mode="json")
        if qa_result.status == "qa_blocked" or review_result.status in {"blocked", "error"}:
            reason = "QA or Review is blocked or unavailable."
        elif qa_result.status == "qa_passed" and (
            qa_result.exit_code != 0
            or qa_result.passed_tests < 1
            or qa_result.failed_tests
            or qa_result.total_tests
            != qa_result.passed_tests + qa_result.failed_tests + qa_result.skipped_tests
        ):
            reason = "QA success lacks executed passing test evidence."
        elif (
            qa_result.status == "qa_failed"
            or review_result.status == "changes_requested"
            or review_result.blocking_findings_count
        ):
            status, reason = (
                "validation_failed",
                "QA failures or review changes require correction.",
            )
        elif qa_result.status == "qa_passed" and review_result.status == "approved":
            status = "validation_passed"
        else:
            reason = "Inconsistent validation result."
    except (ValueError, TypeError, KeyError):
        reason = "Missing, invalid, or mismatched revision validation results."
    try:
        release_revision(state)
    except (ValueError, TypeError, KeyError, OSError):
        status, reason = (
            "validation_blocked",
            "Managed source or validation lease changed during validation.",
        )
    qa = qa or {}
    assessed = assess_qa({**state, "qa_results": qa, "status": qa.get("status", "qa_blocked")})
    feedback = assessed["qa_feedback"]
    findings = [
        f for f in (review or {}).get("findings", []) if f.get("severity") != "informational"
    ]
    feedback["review_findings"] = findings if status == "validation_failed" else []
    if feedback["review_findings"] and qa.get("status") == "qa_passed":
        feedback["category"] = "review_finding"
    # Only test failures with named evidence and evidence-validated review findings are actionable.
    actionable = status == "validation_failed" and (
        feedback["category"] == "test_failure" or bool(feedback["review_findings"])
    )
    if qa.get("status") == "qa_failed" and feedback["category"] != "test_failure":
        actionable = False  # Configuration/timeouts cannot be repaired by source generation.
    result = {
        "status": status,
        "revision": state.get("validation_revision"),
        "retry_attempt": state.get("qa_repair_count", 0),
        "reason": reason,
        "qa_status": qa.get("status"),
        "review_status": (review or {}).get("status"),
        "blocking_findings_count": (review or {}).get("blocking_findings_count", 0),
        "actionable": actionable,
        "final_human_approval": False,
    }
    history = [
        *assessed["recovery_history"],
        redact(
            {
                "stage": "validation",
                "attempt": state.get("qa_repair_count", 0),
                "revision": state.get("validation_revision"),
                "review_result": review,
                "validation_result": result,
                "outcome": status,
            }
        ),
    ]
    return {
        "qa_results": qa,
        "review_result": review,
        "validation_result": result,
        "status": status,
        "error": reason or None,
        "qa_feedback": feedback,
        "recovery_history": history,
    }


def route_combined(state: dict) -> str:
    if (
        state["status"] == "validation_failed"
        and state.get("qa_local_authorized") is True
        and state.get("validation_result", {}).get("actionable")
        and state.get("qa_repair_count", 0) < MAX_REPAIRS
    ):
        return "repair_begin"
    return "validation_finish"


def finish_validation(state: dict) -> dict:
    """Record a validation stopping point without implying human consent or final delivery."""
    status = state["status"]
    if status == "qa_failed":
        status = "validation_failed"  # Rejected Developer repair; do not rerun either branch.
    reason = (
        None
        if status == "validation_passed"
        else (
            "Automatic repair budget exhausted."
            if state.get("qa_repair_count", 0) >= MAX_REPAIRS
            else state.get("error") or "Validation stopped for human review."
        )
    )
    result = {
        **(state.get("validation_result") or {}),
        "status": status,
        "final_outcome": status,
        "failure_reason": reason,
        "final_human_approval": False,
    }
    event = {
        "stage": "validation_final",
        "attempt": state.get("qa_repair_count", 0),
        "revision": state.get("validation_revision"),
        "outcome": status,
        "reason": reason,
    }
    return {
        "status": status,
        "failure_reason": reason,
        "validation_result": result,
        "recovery_history": [*state.get("recovery_history", []), event],
    }
