"""Export checkpoint state into reviewable per-run artifacts, without source execution."""

import json
from pathlib import Path

from orchestrator.design_validation import redact
from orchestrator.file_writer import atomic_write
from orchestrator.observability import RunObserver

REPORT_FIELDS = (
    "requirement",
    "repository_path",
    "project_questions",
    "project_input_kind",
    "project_mode",
    "repository_analysis",
    "requirements",
    "error",
    "design",
    "plan",
    "development_results",
    "qa_result",
    "qa_results",
    "review_result",
    "validation_result",
    "validation_revision",
    "design_validation_status",
    "failure_reason",
    "approval_request",
    "human_approval",
    "final_manifest",
)
LIST_FIELDS = (
    "project_input_history",
    "clarifications",
    "development_issues",
    "design_validation_issues",
    "recovery_history",
    "approval_history",
)
ARTIFACTS = {
    "repository_analysis": "repository_analysis.json",
    "requirements": "requirements.json",
    "design": "design.json",
    "plan": "plan.json",
    "development_results": "development_results.json",
    "qa_results": "qa_results.json",
    "review_result": "review_results.json",
    "validation_result": "validation_results.json",
    "approval_request": "approval_request.json",
    "approval_history": "approval_history.json",
    "final_manifest": "final_manifest.json",
}


def make_report(result: dict, run_id: str) -> dict:
    """Build the compatibility JSON report; interrupt data stays explicit and durable."""
    report = {
        "run_id": run_id,
        "thread_id": run_id,
        "status": result["status"],
        "through": result.get("through", "requirements"),
        **{field: result.get(field) for field in REPORT_FIELDS},
        **{field: result.get(field, []) for field in LIST_FIELDS},
        "qa_repair_count": result.get("qa_repair_count", 0),
        "human_change_count": result.get("human_change_count", 0),
        "design_retry_count": result.get("design_retry_count", 0),
        "design_max_retries": result.get("design_max_retries", 2),
    }
    if result.get("__interrupt__"):
        report["status"] = "paused"
        report["pause"] = dict(result["__interrupt__"][0].value)
        if report["pause"].get("kind") in {"repository", "project_intent"}:
            option = (
                "--repo-path /path/to/application"
                if report["pause"]["kind"] == "repository"
                else '--answers "Build a new application"'
            )
            report["pause"]["resume_command"] = f"python run.py resume --run-id {run_id} {option}"
            report["pause"]["cancel_command"] = f"python run.py resume --run-id {run_id} --reject"
        if "open_questions" in report["pause"]:
            report["open_questions"] = report["pause"]["open_questions"]
    return report


def save_report(report: dict, directory: Path, observer: RunObserver) -> None:
    """Atomically export current artifacts, replacing obsolete results with null."""

    def write(name: str, value) -> None:
        atomic_write(directory / name, json.dumps(value, indent=2, ensure_ascii=False) + "\n")
        observer.emit(
            "artifact_created",
            stage="artifacts",
            artifact=name,
            revision_id=report.get("validation_revision"),
        )

    for field, name in ARTIFACTS.items():
        value = report.get(field)
        if value is not None or (directory / name).exists():
            write(name, value)
    history = redact(report.get("recovery_history", []))
    if history:
        for attempt in history:
            if attempt.get("stage") is None:
                write(f"design_attempts/attempt_{attempt['attempt']:03d}.json", attempt)
        write("recovery_history.json", history)
    write("status.json", report)
