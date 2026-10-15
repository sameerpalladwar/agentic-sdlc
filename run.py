import argparse
import json
import re
import sqlite3
import sys
from pathlib import Path
from uuid import uuid4

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.types import Command

from orchestrator.graph import build_graph


RUNS_DIR = Path(__file__).resolve().parent / "workspace" / "runs"


def execute(args: argparse.Namespace) -> dict:
    """Use the run ID as both the checkpoint thread ID and artifact directory."""
    run_id = uuid4().hex if args.command == "start" else args.run_id
    if not re.fullmatch(r"[a-f0-9]{32}", run_id):
        raise ValueError("Run ID must be the 32-character ID returned by start.")
    root = RUNS_DIR.resolve()
    run_dir = root / run_id
    if run_dir.is_symlink():
        raise ValueError("Run directory must not be a symlink.")
    if args.command == "start":
        if not args.requirement.strip():
            raise ValueError("Requirement must not be empty.")
        run_dir.mkdir(parents=True, exist_ok=False)
    elif not (run_dir / "checkpoints.sqlite").is_file():
        raise ValueError("Unknown run: no persistent checkpoint found.")
    for name in ("checkpoints.sqlite", "requirements.json", "design.json", "plan.json", "status.json"):
        if (run_dir / name).is_symlink():
            raise ValueError("Run artifacts must not be symlinks.")

    config = {"configurable": {"thread_id": run_id}}
    with SqliteSaver.from_conn_string(str(run_dir / "checkpoints.sqlite")) as saver:
        graph = build_graph(saver)
        if args.command == "start":
            result = graph.invoke({"requirement": args.requirement, "status": "pending", "through": args.through}, config)
        else:
            snapshot = graph.get_state(config)
            if not any(task.interrupts for task in snapshot.tasks):
                raise ValueError("Run is not paused for clarification; it cannot be resumed.")
            if args.answers is not None and not args.answers.strip():
                raise ValueError("Answers must not be empty.")
            decision = {"reject": True} if args.reject else {"answers": args.answers}
            result = graph.invoke(Command(resume=decision), config)
        report = {"run_id": run_id, "thread_id": run_id, "status": result["status"],
                  "requirements": result.get("requirements"), "error": result.get("error"),
                  "clarifications": result.get("clarifications", []),
                  "through": result.get("through", "requirements"), "design": result.get("design"),
                  "plan": result.get("plan")}
        if result.get("__interrupt__"):
            report["status"] = "paused"
            report["open_questions"] = result["__interrupt__"][0].value["open_questions"]

    if report["requirements"] is not None or (run_dir / "requirements.json").exists():
        (run_dir / "requirements.json").write_text(
            json.dumps(report["requirements"], indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    if report["design"] is not None or (run_dir / "design.json").exists():
        (run_dir / "design.json").write_text(
            json.dumps(report["design"], indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    if report["plan"] is not None or (run_dir / "plan.json").exists():
        (run_dir / "plan.json").write_text(
            json.dumps(report["plan"], indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    (run_dir / "status.json").write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Start or resume a requirements run.")
    commands = parser.add_subparsers(dest="command", required=True)
    start = commands.add_parser("start", help="Start a new persistent run")
    start.add_argument("--requirement", required=True)
    start.add_argument("--through", "--stop-after", choices=["requirements", "design", "planning"], default="requirements",
                       help="Final stage to execute (persisted for resume)")
    resume = commands.add_parser("resume", help="Answer questions or reject a paused run")
    resume.add_argument("--run-id", required=True)
    choice = resume.add_mutually_exclusive_group(required=True)
    choice.add_argument("--answers", help="Answers to the open questions as plain text")
    choice.add_argument("--reject", action="store_true", help="End the run without another LLM call")
    arguments = list(sys.argv[1:] if argv is None else argv)
    if arguments and arguments[0] == "--requirement":
        arguments.insert(0, "start")  # Retain the Stage 1 CLI shorthand.
    args = parser.parse_args(arguments)
    try:
        report = execute(args)
    except (OSError, sqlite3.Error):
        print(json.dumps({"status": "failed", "error": "Unable to save or load the run in workspace/runs."}), file=sys.stderr)
        return 1
    except ValueError as exc:
        print(json.dumps({"status": "failed", "error": str(exc)}), file=sys.stderr)
        return 1
    print(json.dumps(report, indent=2, ensure_ascii=False))
    return 1 if report["status"] == "failed" else 0


if __name__ == "__main__":
    raise SystemExit(main())
