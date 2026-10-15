"""CLI entry point for checkpointed generation, validation, human decisions, and delivery."""

import argparse
import json
import re
import sqlite3
import sys
import time
from pathlib import Path
from uuid import uuid4

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.types import Command

from orchestrator import file_writer
from orchestrator.artifacts import ARTIFACTS, make_report, save_report
from orchestrator.design_validation import redact
from orchestrator.graph import build_graph
from orchestrator.observability import RunObserver
from orchestrator.progress import ProgressRenderer

RUNS_DIR = Path(__file__).resolve().parent / "workspace" / "runs"
VALIDATION_STATUSES = {
    "development_complete",
    "qa_passed",
    "qa_failed",
    "qa_blocked",
    "qa_error",
    "validation_passed",
    "validation_failed",
    "validation_blocked",
}
REPLAY_STATUSES = VALIDATION_STATUSES | {
    "developing",
    "repairing",
    "validating",
    "approved",
    "human_changes_requested",
}


def run_directory(args: argparse.Namespace) -> tuple[str, Path]:
    """Resolve a known run or create a fresh run, rejecting unsafe artifact destinations."""
    run_id = uuid4().hex if args.command == "start" else args.run_id
    if not re.fullmatch(r"[a-f0-9]{32}", run_id):
        raise ValueError("Run ID must be the 32-character ID returned by start.")
    directory = RUNS_DIR.resolve() / run_id
    file_writer.reject_symlinks(directory)
    if args.command == "start":
        if not args.requirement.strip():
            raise ValueError("Requirement must not be empty.")
        directory.mkdir(parents=True, exist_ok=False)
    elif not (directory / "checkpoints.sqlite").is_file():
        raise ValueError("Unknown run: no persistent checkpoint found.")
    for name in {"checkpoints.sqlite", "status.json", "recovery_history.json", *ARTIFACTS.values()}:
        file_writer.reject_symlinks(directory / name)
    return run_id, directory


def approval_decision(args: argparse.Namespace, snapshot) -> dict:
    """Validate CLI feedback before consuming an interrupt, preserving invalid requests."""
    request = snapshot.values.get("approval_request") or {}
    if snapshot.values.get("status") != "awaiting_approval" or not any(
        task.interrupts for task in snapshot.tasks
    ):
        raise ValueError("Run is not awaiting human approval or already has a decision.")
    if args.revision != request.get("revision"):
        raise ValueError("Stale revision: inspect approval_request.json before deciding.")
    if args.decision == "request_changes" and not (args.feedback or "").strip():
        raise ValueError("Request changes requires nonempty --feedback.")
    if args.files and args.decision != "request_changes":
        raise ValueError("--file is only valid with request_changes.")
    if args.files:
        from orchestrator.qa_recovery import allowed_sources

        files = snapshot.values.get("validation_files", {})
        allowed = allowed_sources(
            files, {"human_paths": args.files, "failing_tests": [], "traceback_summary": ""}
        )
        if not set(args.files) <= allowed:
            raise ValueError(
                "Only existing application source files may be selected; test edits require separate review."
            )
    return {
        "decision": args.decision,
        "revision": args.revision,
        "request_id": request["request_id"],
        "feedback": args.feedback or "",
        "files": args.files,
    }


def resume_input(args: argparse.Namespace, snapshot):
    """Preserve clarification/development resumes; replay pending nodes after interruption."""
    interrupted = any(task.interrupts for task in snapshot.tasks)
    if not interrupted:
        if (
            args.retry_development
            and snapshot.values.get("status") in REPLAY_STATUSES
            and snapshot.next
        ):
            return None
        raise ValueError(
            "Run is not paused for clarification or development; it cannot be resumed."
        )
    if snapshot.values.get("status") == "awaiting_approval":
        raise ValueError("Use decide --revision --decision for the pending human approval.")
    if args.answers is not None and not args.answers.strip():
        raise ValueError("Answers must not be empty.")
    development = snapshot.values.get("status") == "development_paused"
    if development and not (args.retry_development or args.reject):
        raise ValueError("Use --retry-development or --reject for a development pause.")
    if not development and args.retry_development:
        raise ValueError("This run is paused for clarification, not development.")
    decision = (
        {"reject": True}
        if args.reject
        else ({"retry_development": True} if args.retry_development else {"answers": args.answers})
    )
    return Command(resume=decision)


def invoke_run(
    graph,
    args: argparse.Namespace,
    run_id: str,
    directory: Path,
    config: dict,
    observer: RunObserver,
) -> dict:
    """Select a start/resume operation while keeping identity and checkpoint routing stable."""
    if args.command == "start":
        return graph.invoke(
            {
                "requirement": args.requirement,
                "status": "pending",
                "through": args.through,
                "run_id": run_id,
                "run_directory": str(directory),
                "development_budget": args.development_budget,
                "design_retry_count": 0,
                "design_max_retries": 2,
                "recovery_history": [],
            },
            config,
        )
    snapshot = graph.get_state(config)
    if args.command == "qa":
        if snapshot.values.get("status") not in VALIDATION_STATUSES:
            raise ValueError("QA requires a run with completed development.")
        through = args.through or (
            "delivery" if snapshot.values.get("through") == "delivery" else "qa"
        )
        graph.update_state(
            config,
            {
                "through": through,
                "status": "development_complete",
                "active_task": None,
                "qa_local_authorized": args.authorize_local_execution,
                "run_directory": str(directory),
                "application_dir": str(file_writer.OUTPUT_ROOT / run_id),
            },
            as_node="development_select",
        )
        return graph.invoke(None, config)
    if args.command == "decide":
        decision = approval_decision(args, snapshot)
        observer.emit(
            "human_approval_resume",
            stage="human_approval",
            revision_id=args.revision,
            request_id=decision["request_id"],
        )
        return graph.invoke(Command(resume=decision), config)
    if (
        snapshot.values.get("status") == "completed"
        and args.retry_development
        and not snapshot.next
    ):
        # A process may stop after the delivery checkpoint but before exporting artifacts.
        return dict(snapshot.values)
    return graph.invoke(resume_input(args, snapshot), config)


def execute(args: argparse.Namespace) -> dict:
    """Run with append-only observability and export artifacts from durable graph state."""
    run_id, directory = run_directory(args)
    progress = ProgressRenderer(enabled=not args.no_progress, verbose=args.verbose)
    observer = RunObserver(run_id, directory, progress)
    started = time.monotonic()
    observer.emit("run_started", stage="workflow", command=args.command)
    try:
        config = {"configurable": {"thread_id": run_id}, "recursion_limit": 250}
        with SqliteSaver.from_conn_string(str(directory / "checkpoints.sqlite")) as saver:
            result = invoke_run(build_graph(saver), args, run_id, directory, config, observer)
        report = make_report(result, run_id)
        save_report(report, directory, observer)
        observer.emit(
            "run_completed",
            stage="workflow",
            status=report["status"],
            revision_id=report.get("validation_revision"),
            duration_seconds=round(time.monotonic() - started, 6),
        )
        return report
    except BaseException as error:
        observer.exception("workflow", error)
        observer.emit(
            "run_failed",
            stage="workflow",
            status="failed",
            duration_seconds=round(time.monotonic() - started, 6),
        )
        raise
    finally:
        observer.close()


def parser() -> argparse.ArgumentParser:
    """Keep legacy subcommands and add explicit output controls and revision-bound decisions."""
    options = argparse.ArgumentParser(add_help=False)
    options.add_argument(
        "--json",
        action="store_true",
        default=argparse.SUPPRESS,
        help="Print the complete compatibility JSON report (may be large)",
    )
    options.add_argument(
        "--verbose",
        action="store_true",
        default=argparse.SUPPRESS,
        help="Show diagnostic durations; never raw model payloads",
    )
    options.add_argument(
        "--no-progress",
        action="store_true",
        default=argparse.SUPPRESS,
        help="Disable progress output to stderr",
    )
    cli = argparse.ArgumentParser(
        description="Run the checkpointed Agentic SDLC workflow.", parents=[options]
    )
    commands = cli.add_subparsers(dest="command", required=True)
    start = commands.add_parser("start", parents=[options], help="Start a new persistent run")
    start.add_argument("--requirement", required=True)
    start.add_argument(
        "--through",
        "--stop-after",
        choices=[
            "requirements",
            "design",
            "planning",
            "development",
            "qa",
            "validation",
            "delivery",
        ],
        default="requirements",
    )
    start.add_argument(
        "--development-budget", type=int, choices=range(1, 51), default=20, metavar="1..50"
    )
    resume = commands.add_parser(
        "resume", parents=[options], help="Resume clarification or pending checkpoint work"
    )
    resume.add_argument("--run-id", required=True)
    choice = resume.add_mutually_exclusive_group(required=True)
    choice.add_argument("--answers")
    choice.add_argument("--retry-development", action="store_true")
    choice.add_argument("--reject", action="store_true")
    qa = commands.add_parser(
        "qa", parents=[options], help="Run parallel QA/Review on explicitly trusted code"
    )
    qa.add_argument("--run-id", required=True)
    qa.add_argument(
        "--authorize-local-execution", action="store_true", help="A venv is NOT a sandbox"
    )
    qa.add_argument("--through", "--stop-after", choices=["qa", "validation", "delivery"])
    decide = commands.add_parser(
        "decide", parents=[options], help="Answer a pending human approval request"
    )
    decide.add_argument("--run-id", required=True)
    decide.add_argument("--revision", required=True)
    decide.add_argument(
        "--decision", required=True, choices=["approve", "request_changes", "reject"]
    )
    decide.add_argument("--feedback")
    decide.add_argument(
        "--file",
        dest="files",
        action="append",
        help="Existing source path to scope request_changes",
    )
    return cli


def print_summary(report: dict) -> None:
    """Print a compact result, approval context, and paths instead of the complete state."""
    report = redact(report)
    print(f"Run {report['run_id']}: {report['status']}")
    if report.get("validation_revision"):
        print(f"Revision: {report['validation_revision']}")
    qa = report.get("qa_results")
    if qa:
        print(
            f"QA: {qa['status']} ({qa['passed_tests']} passed, {qa['failed_tests']} failed, {qa['skipped_tests']} skipped)"
        )
    review = report.get("review_result")
    if review:
        print(f"Review: {review['status']} ({review['blocking_findings_count']} blocking findings)")
    if report.get("pause", {}).get("kind") == "approval":
        request = report["approval_request"]
        print(f"Requirement: {request['requirement_summary']}")
        print(f"Code: {request['application_path']}")
        print(f"Review summary: {request['review_summary']['summary']}")
        for limitation in request["limitations"]:
            print(f"Limitation: {limitation}")
        print(
            "Approval: use decide --run-id <id> --revision <revision> --decision approve|request_changes|reject"
        )
    elif report.get("open_questions"):
        for question in report["open_questions"]:
            print(f"Question: {question}")
    if report.get("error"):
        print(f"Details: {report['error']}")
    directory = RUNS_DIR / report["run_id"]
    print(f"Artifacts: {directory}")
    if report.get("final_manifest"):
        print(f"Delivered code: {report['final_manifest']['generated_application_path']}")
        print(f"Final manifest: {directory / 'final_manifest.json'}")


def main(argv: list[str] | None = None) -> int:
    """Parse CLI input, preserve machine JSON on opt-in, and return stable exit codes."""
    arguments = list(sys.argv[1:] if argv is None else argv)
    index = 0
    while index < len(arguments) and arguments[index] in {"--json", "--verbose", "--no-progress"}:
        index += 1
    if index < len(arguments) and arguments[index] == "--requirement":
        arguments.insert(index, "start")
    args = parser().parse_args(arguments)
    for name in ("json", "verbose", "no_progress"):
        if not hasattr(args, name):
            setattr(args, name, False)
    try:
        report = execute(args)
    except (OSError, sqlite3.Error):
        error = "Unable to save or load the run in workspace/runs."
    except ValueError as exception:
        error = str(exception)
    else:
        if args.json:
            print(json.dumps(report, indent=2, ensure_ascii=False))
        else:
            print_summary(report)
        if report["status"] in {"failed", "qa_failed", "qa_error", "validation_failed"}:
            return 1
        return (
            3 if report["status"] in {"qa_blocked", "validation_blocked", "approval_blocked"} else 0
        )
    if args.json:
        print(json.dumps({"status": "failed", "error": error}), file=sys.stderr)
    else:
        print(f"Failed: {error}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
