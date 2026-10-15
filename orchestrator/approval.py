"""Revision-bound human decisions and final delivery; never deploy application code."""

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

from langgraph.types import interrupt

from agents.reviewer import validate_review
from models.qa import QAResult
from models.requirements import RequirementsOutput
from models.review import CodeReviewResult
from orchestrator.design_validation import redact
from orchestrator.file_writer import SafeWorkspace, atomic_write
from orchestrator.qa_recovery import allowed_sources, repair_developer
from orchestrator.revision import frozen_files, revision_id

LIMITATIONS = [
    "Local QA is explicitly authorized but is not sandboxed.",
    "Acceptance criteria remain not_verified without test-to-story evidence.",
    "LLM code review and static safety checks are fallible.",
    "No deployment or generated-code publication is performed.",
]


def timestamp() -> str:
    """Return an explicit UTC audit timestamp."""
    return datetime.now(timezone.utc).isoformat()


def verify_approval_revision(state: dict, workspace: SafeWorkspace) -> str:
    """Check live source and both independent verdicts before a decision or delivery."""
    revision = state["validation_revision"]
    files = frozen_files(state)
    if workspace.manifest.get("pending") or revision_id(workspace.snapshot()) != revision:
        raise ValueError("Live source differs from the validated revision.")
    requirements = RequirementsOutput.model_validate(state["requirements"])
    qa = QAResult.model_validate(state["qa_result"])
    review = validate_review(
        CodeReviewResult.model_validate(state["review_result"]),
        files,
        revision,
        {story.id for story in requirements.user_stories},
    )
    validation = state["validation_result"]
    attempt = state.get("qa_repair_count", 0)
    if (
        requirements.requires_human_clarification
        or state.get("qa_local_authorized") is not True
        or validation["status"] != "validation_passed"
        or validation["revision"] != revision
        or qa.revision != revision
        or qa.retry_attempt != attempt
        or review.retry_attempt != attempt
        or validation["retry_attempt"] != attempt
        or qa.status != "qa_passed"
        or qa.exit_code != 0
        or qa.passed_tests < 1
        or qa.failed_tests
        or qa.total_tests != qa.passed_tests + qa.failed_tests + qa.skipped_tests
        or review.status != "approved"
        or review.blocking_findings_count
    ):
        raise ValueError("Approval requires passing QA and Review for the current revision.")
    return revision


def approval_prepare(state: dict) -> dict:
    """Persist a write barrier and a stable request ID before the interrupt checkpoint."""
    try:
        with SafeWorkspace(state["run_id"]) as workspace:
            revision = verify_approval_revision(state, workspace)
            request_id = hashlib.sha256(
                f"{state['run_id']}:{revision}:{len(state.get('approval_history', []))}".encode()
            ).hexdigest()
            if workspace.manifest.get("approval_request_id") not in {None, request_id}:
                raise ValueError("Another human approval is pending.")
            workspace.manifest.update(approval_revision=revision, approval_request_id=request_id)
            atomic_write(workspace.manifest_path, json.dumps(workspace.manifest))
            application_path = str(workspace.root.resolve())
        request = {
            "kind": "approval",
            "request_id": request_id,
            "revision": revision,
            "requirement_summary": state["requirements"]["summary"],
            "application_path": application_path,
            "qa_summary": {
                key: state["qa_result"][key]
                for key in ("status", "passed_tests", "failed_tests", "skipped_tests")
            },
            "review_summary": {
                "status": state["review_result"]["status"],
                "summary": state["review_result"]["summary"],
                "blocking_findings_count": state["review_result"]["blocking_findings_count"],
            },
            "limitations": LIMITATIONS
            + state.get("design", {}).get("risks", [])
            + [
                f"Review {finding['severity']}: {finding['description']}"
                for finding in state["review_result"]["findings"]
            ],
        }
        return {"status": "awaiting_approval", "approval_request": redact(request), "error": None}
    except (ValueError, TypeError, KeyError, OSError):
        return {
            "status": "approval_blocked",
            "error": "Cannot approve stale or incomplete validation.",
        }


def decision_update(state: dict, record: dict) -> dict:
    """Translate a journaled decision into checkpoint state, invalidating changed work."""
    history = [*state.get("approval_history", []), record]
    base = {"approval_history": history, "human_approval": record, "error": None}
    if record["decision"] == "approve":
        return {**base, "status": "approved"}
    if record["decision"] == "reject":
        return {**base, "status": "rejected"}
    feedback = {
        "category": "human_feedback",
        "human_feedback": record["feedback"],
        "human_paths": record["files"],
        "failing_tests": [],
        "traceback_summary": "",
        "error_summary": "Human requested targeted source changes.",
    }
    return {
        **base,
        "status": "human_changes_requested",
        "qa_feedback": feedback,
        "human_change_count": state.get("human_change_count", 0) + 1,
        "qa_result": None,
        "qa_results": None,
        "review_result": None,
        "validation_result": None,
        "approval_request": None,
    }


def human_approval(state: dict) -> dict:
    """Interrupt without terminal input; consume a revision-bound decision on resume."""
    request = state["approval_request"]
    decision = interrupt(request)
    try:
        if (
            not isinstance(decision, dict)
            or decision.get("decision") not in {"approve", "request_changes", "reject"}
            or decision.get("revision") != request["revision"]
            or decision.get("request_id") != request["request_id"]
        ):
            raise ValueError("Stale or invalid human decision.")
        if any(
            record["request_id"] == request["request_id"]
            for record in state.get("approval_history", [])
        ):
            raise ValueError("This request already has a decision.")
        if not isinstance(decision.get("feedback", ""), str):
            raise ValueError("Feedback must be text.")
        feedback = decision.get("feedback", "").strip()
        if decision["decision"] == "request_changes" and not feedback:
            raise ValueError("Request changes requires feedback.")
        with SafeWorkspace(state["run_id"]) as workspace:
            # Rejection is always a safe stop, even if live source is no longer valid.
            allowed = set()
            if decision["decision"] != "reject":
                verify_approval_revision(state, workspace)
                files = workspace.snapshot()
                selected = decision.get("files") or [path for path in files if path.endswith(".py")]
                if not isinstance(selected, list) or not all(
                    isinstance(path, str) for path in selected
                ):
                    raise ValueError("Invalid source selection.")
                allowed = allowed_sources(
                    files, {"failing_tests": [], "traceback_summary": "", "human_paths": selected}
                )
                if decision["decision"] == "request_changes" and (
                    not allowed or set(selected) - allowed and decision.get("files")
                ):
                    raise ValueError("Human source changes cannot edit tests or unsafe paths.")
            journal = workspace.manifest.setdefault("approval_decisions", {})
            record = journal.get(request["request_id"])
            if record is None:
                if workspace.manifest.get("approval_request_id") != request["request_id"]:
                    raise ValueError("Pending approval identity changed.")
                record = {
                    "request_id": request["request_id"],
                    "revision": request["revision"],
                    "decision": decision["decision"],
                    "feedback": feedback,
                    "files": sorted(allowed) if decision["decision"] == "request_changes" else [],
                    "timestamp": timestamp(),
                }
                journal[request["request_id"]] = record
            elif record["decision"] != decision["decision"] or record["feedback"] != feedback:
                raise ValueError("A different decision is already journaled.")
            # The journal makes a process crash before checkpointing replay-safe.
            if record["decision"] != "approve":
                workspace.manifest.pop("approval_revision", None)
                workspace.manifest.pop("approval_request_id", None)
            atomic_write(workspace.manifest_path, json.dumps(workspace.manifest))
        return decision_update(state, record)
    except (ValueError, TypeError, KeyError, OSError):
        return {
            "status": "approval_blocked",
            "error": "Decision blocked: stale revision, invalid feedback, or consumed request.",
        }


def human_repair(state: dict) -> dict:
    """Reuse source-only repair safety; human requests do not reset automatic budgets."""
    return repair_developer({**state, "repair_mode": "human"})


def final_delivery(state: dict) -> dict:
    """Produce checkpointed delivery metadata; the CLI exports it without deploying."""
    try:
        record = state["human_approval"]
        with SafeWorkspace(state["run_id"]) as workspace:
            revision = verify_approval_revision(state, workspace)
            if (
                state["status"] != "approved"
                or record["decision"] != "approve"
                or record["revision"] != revision
                or workspace.manifest.get("approval_decisions", {}).get(record["request_id"])
                != record
            ):
                raise ValueError("Delivery requires a matching journaled human approval.")
            workspace.manifest["delivery_revision"] = revision
            workspace.manifest.pop("approval_revision", None)
            workspace.manifest.pop("approval_request_id", None)
            atomic_write(workspace.manifest_path, json.dumps(workspace.manifest))
            application_path = str(workspace.root.resolve())
        run_directory = Path(state.get("run_directory", f"workspace/runs/{state['run_id']}"))
        manifest = {
            "run_id": state["run_id"],
            "approved_revision": revision,
            "requirement_artifact": str(run_directory / "requirements.json"),
            "user_stories_artifact": str(run_directory / "requirements.json"),
            "design_artifact": str(run_directory / "design.json"),
            "plan_artifact": str(run_directory / "plan.json"),
            "generated_application_path": application_path,
            "qa_summary": state["approval_request"]["qa_summary"],
            "code_review_summary": state["approval_request"]["review_summary"],
            "human_approval": record,
            "completion_timestamp": timestamp(),
            "known_limitations": state["approval_request"]["limitations"],
        }
        return {
            "status": "completed",
            "final_manifest": manifest,
            "error": None,
            "validation_result": {
                **state["validation_result"],
                "final_human_approval": True,
                "approved_revision": revision,
            },
        }
    except (ValueError, TypeError, KeyError, OSError):
        return {
            "status": "approval_blocked",
            "error": "Delivery blocked: approval or source revision changed.",
        }
