"""Thread-safe terminal progress without raw state, model output, or input prompts."""

import os
import sys
import threading
from typing import TextIO

LABELS = {
    "clarification": "Requirements: Waiting for human clarification",
    "development_gate": "Developer: Waiting for retry or rejection",
    "project_routing": "Project: Determining workflow",
    "project_input": "Project: Waiting for repository or intent clarification",
    "repository_analysis": "Repository Analysis: Inspecting existing application",
    "requirements": "Requirements: Generating user stories",
    "design": "Design: Generating architecture",
    "design_validator": "Design: Validating architecture",
    "design_retry": "Design: Correcting validation issues",
    "planning": "Planning: Creating task DAG",
    "developer": "Developer: Implementing task",
    "qa": "QA: Running automated tests",
    "review": "Code Review: Checking security and maintainability",
    "validation_gate": "Validation: Combining QA and Review",
    "validation_snapshot": "Validation: Freezing code revision",
    "repair_begin": "Developer: Retrying targeted repair",
    "developer_repair": "Developer: Repairing validation defects",
    "human_repair": "Developer: Applying human feedback",
    "approval_prepare": "Approval: Preparing revision summary",
    "human_approval": "Approval: Waiting for human decision",
    "delivery": "Delivery: Preparing final manifest",
}


class ProgressRenderer:
    """Keep one active line in ANSI terminals; use compact lines in CI/plain terminals."""

    def __init__(
        self,
        stream: TextIO | None = None,
        *,
        enabled: bool = True,
        verbose: bool = False,
        ansi: bool | None = None,
    ):
        self.stream = stream or sys.stderr
        self.enabled = enabled
        self.verbose = verbose
        self.ansi = (
            (self.stream.isatty() and os.getenv("TERM") != "dumb" and "NO_COLOR" not in os.environ)
            if ansi is None
            else ansi
        )
        self.active: dict[str, str] = {}
        self.lock = threading.Lock()

    def render(self, stage: str, status: str, detail: str = "") -> None:
        """Serialize parallel branch updates and retain one summary per finished stage."""
        if not self.enabled or stage not in LABELS:
            return
        with self.lock:
            try:
                label = LABELS[stage]
                if stage == "human_approval" and status == "completed":
                    label = "Approval: Human decision recorded"
                if stage == "human_approval" and status == "failed":
                    label = "Approval: Revision or decision check failed"
                message = label + (f" {detail}" if detail else "")
                if self.ansi:
                    self.stream.write("\r\033[2K")
                if status == "running":
                    self.active[stage] = message
                    if not self.ansi:
                        if stage in {"qa", "review"} and len(self.active) > 1:
                            message = "Validation: Waiting for QA and Review | " + " | ".join(
                                self.active.values()
                            )
                        self.stream.write(f"[running] {message}\n")
                else:
                    self.active.pop(stage, None)
                    self.stream.write(f"[{status}] {message}\n")
                if self.ansi and self.active:
                    self.stream.write("[running] " + " | ".join(self.active.values()))
                self.stream.flush()
            except (OSError, ValueError):
                pass  # A closed pipe must not change graph execution or approval state.

    def close(self) -> None:
        """Finish any active status line before a CLI summary or interrupt response."""
        with self.lock:
            if self.enabled and self.ansi and self.active:
                try:
                    self.stream.write("\n")
                    self.stream.flush()
                except (OSError, ValueError):
                    pass
            self.active.clear()
