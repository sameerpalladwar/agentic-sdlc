"""Per-run standard-library logs; event metadata never contains raw model/state payloads."""

import functools
import json
import logging
import os
import re
import threading
import time
import traceback
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path

from langgraph.errors import GraphInterrupt

from orchestrator.file_writer import reject_symlinks
from orchestrator.progress import ProgressRenderer

OBSERVERS: dict[str, "RunObserver"] = {}
REGISTRY_LOCK = threading.Lock()
FIELDS = {
    "stage",
    "attempt",
    "revision_id",
    "duration_seconds",
    "status",
    "command",
    "previous_status",
    "error_type",
    "stack_trace",
    "artifact",
    "request_id",
    "task_id",
}


def redact_text(text: str) -> str:
    """Redact before JSON encoding so quoted/newline-containing secrets cannot escape masking."""
    for name, value in os.environ.items():
        if len(value) >= 4 and re.search(r"KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL|AUTH", name, re.I):
            text = text.replace(value, "[REDACTED]")
    text = re.sub(r"sk-[A-Za-z0-9_-]{12,}|\bBearer\s+\S+", "[REDACTED]", text)
    return re.sub(
        r"(?i)(api[_-]?key|password|authorization|token)\s*[:=]\s*\S+", r"\1=[REDACTED]", text
    )


def safe_metadata(data: dict) -> dict:
    """Whitelist scalar event fields; raw nested payloads are never log metadata."""
    clean = {}
    for key, value in data.items():
        if key not in FIELDS:
            continue
        if isinstance(value, str):
            if key == "task_id" and not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", value):
                clean[key] = "[OMITTED]"
            else:
                clean[key] = redact_text(value)
        elif value is None or isinstance(value, (bool, int, float)):
            clean[key] = value
        else:
            clean[key] = "[OMITTED]"
    return clean


class SafeFileHandler(logging.FileHandler):
    """Logging I/O failure is nonfatal and must not print unsanitized logging diagnostics."""

    def handleError(self, record: logging.LogRecord) -> None:
        pass


class EventFormatter(logging.Formatter):
    """Render a JSONL event or readable timestamped equivalent from sanitized metadata."""

    def __init__(self, structured: bool):
        super().__init__()
        self.structured = structured

    def format(self, record: logging.LogRecord) -> str:
        event = {
            "timestamp": datetime.fromtimestamp(record.created, timezone.utc).isoformat(),
            "severity": record.levelname,
            **record.event_data,
        }
        if self.structured:
            return json.dumps(event, ensure_ascii=False)
        details = " ".join(
            f"{key}={value}"
            for key, value in event.items()
            if key not in {"timestamp", "severity", "event"}
        )
        return f"{event['timestamp']} {event['severity']} {event['event']} {details}"


class RunObserver:
    """Own one invocation's append-only handlers and renderer; close them on resume/exit."""

    def __init__(self, run_id: str, directory: Path, progress: ProgressRenderer):
        self.run_id = run_id
        self.directory = directory
        self.progress = progress
        self.logger = logging.Logger(f"agentic_sdlc.{run_id}", logging.INFO)
        self.logger.propagate = False
        try:
            reject_symlinks(directory)
            for name, structured in [("execution.log", False), ("events.jsonl", True)]:
                path = directory / name
                reject_symlinks(path)
                if path.exists() and (not path.is_file() or path.stat().st_nlink != 1):
                    raise OSError("Unsafe log destination.")
                handler = SafeFileHandler(path, encoding="utf-8")
                handler.setFormatter(EventFormatter(structured))
                self.logger.addHandler(handler)
        except OSError:
            self.close_handlers()
        except ValueError:
            self.close_handlers()
        if not self.logger.handlers:
            self.logger.addHandler(logging.NullHandler())
        with REGISTRY_LOCK:
            OBSERVERS[run_id] = self

    def emit(self, event: str, *, level: int = logging.INFO, **metadata) -> None:
        """Emit allowlisted metadata only; logs are not the workflow source of truth."""
        try:
            data = {
                "event": redact_text(event),
                "run_id": redact_text(self.run_id),
                **safe_metadata(metadata),
            }
            self.logger.log(level, event, extra={"event_data": data})
        except (OSError, ValueError, TypeError):
            pass

    def exception(self, stage: str, error: BaseException) -> None:
        """Capture stack frames without source text, exception messages, payloads, or locals."""
        frames = "\n".join(
            f"File {frame.filename}, line {frame.lineno}, in {frame.name}"
            for frame in traceback.extract_tb(error.__traceback__)
        )
        self.emit(
            "exception",
            level=logging.ERROR,
            stage=stage,
            error_type=type(error).__name__,
            stack_trace=frames,
        )

    def close_handlers(self) -> None:
        for handler in list(self.logger.handlers):
            self.logger.removeHandler(handler)
            try:
                handler.close()
            except OSError:
                pass

    def close(self) -> None:
        self.progress.close()
        self.close_handlers()
        with REGISTRY_LOCK:
            if OBSERVERS.get(self.run_id) is self:
                OBSERVERS.pop(self.run_id, None)


def observe_node(stage: str, function: Callable[[dict], dict]) -> Callable[[dict], dict]:
    """Instrument a graph node without adding state writes or changing interrupt semantics."""

    @functools.wraps(function)
    def observed(state: dict) -> dict:
        observer = OBSERVERS.get(state.get("run_id"))
        if observer is None:
            return function(state)
        started = time.monotonic()
        metadata = {
            "stage": stage,
            "attempt": state.get("qa_repair_count", 0),
            "revision_id": state.get("validation_revision"),
            "task_id": state.get("active_task"),
        }
        if stage == "repair_begin":
            metadata["attempt"] += 1
        if stage.startswith("design"):
            metadata["attempt"] = state.get("design_retry_count", 0)
        if stage == "human_repair":
            metadata["attempt"] = state.get("human_change_count", 0)
        detail = ""
        if stage == "developer":
            metadata["attempt"] = 1 + sum(
                record.get("task_id") == state.get("active_task")
                for record in state.get("development_results", [])
            )
            tasks = state.get("plan", {}).get("tasks", [])
            index = next(
                (
                    index
                    for index, task in enumerate(tasks, 1)
                    if task["task_id"] == state.get("active_task")
                ),
                0,
            )
            detail = f"{index}/{len(tasks)}"
        observer.emit("agent_started", **metadata)
        if stage in {"qa", "review"}:
            observer.emit("parallel_started", **metadata)
        if stage in {"repair_begin", "human_repair"}:
            observer.emit("repair_attempt", **metadata)
        observer.progress.render(stage, "running", detail)
        try:
            result = function(state)
        except GraphInterrupt:
            observer.emit(
                "human_approval_interrupt"
                if stage == "human_approval"
                else "human_input_interrupt",
                **metadata,
            )
            observer.progress.render(stage, "waiting")
            raise
        except BaseException as error:
            observer.exception(stage, error)
            observer.progress.render(stage, "failed")
            raise
        duration = time.monotonic() - started
        status = result.get("status", "")
        if stage in {"qa", "review"}:
            status = (result.get("qa_result") or result.get("review_result") or {}).get(
                "status", ""
            )
        if stage == "developer" and result.get("development_results"):
            status = result["development_results"][-1]["status"]
        if stage == "design" and result.get("design_generation_issues"):
            status = "failed"
        failed = any(word in status for word in ("failed", "blocked", "error", "invalid"))
        metadata.update(
            duration_seconds=round(duration, 6),
            status=status,
            revision_id=result.get("validation_revision", metadata["revision_id"]),
        )
        observer.emit(
            "agent_failed" if failed else "agent_completed",
            level=logging.WARNING if failed else logging.INFO,
            **metadata,
        )
        if stage in {"qa", "review"}:
            observer.emit("parallel_completed", **metadata)
        if stage == "validation_gate":
            observer.emit("validation_decision", **metadata)
        if stage == "human_approval":
            observer.emit("human_approval_resumed", **metadata)
        if "exhausted" in (result.get("failure_reason") or "").lower():
            observer.emit("retry_exhausted", level=logging.WARNING, **metadata)
        observer.emit("transition", previous_status=state.get("status"), **metadata)
        observer.progress.render(
            stage,
            "failed"
            if failed
            else "retrying"
            if stage in {"repair_begin", "design_retry"}
            else "completed",
            f"({duration:.2f}s)" if observer.progress.verbose else "",
        )
        return result

    return observed
