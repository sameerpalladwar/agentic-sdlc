"""Bounded, checkpointed repairs of application source; tests are immutable."""

import ast
import hashlib
import json
import re
from collections import Counter
from pathlib import PurePosixPath

from agents.developer import generate_task
from orchestrator.dependencies import validate_dependencies
from orchestrator.design_validation import redact
from orchestrator.development import development_inputs
from orchestrator.file_writer import SafeWorkspace

MAX_REPAIRS = 2


def failure_feedback(report: dict) -> dict:
    output = report.get("test_output", "")
    names = re.findall(r"^FAILED ([^\s]+::[^\s]+)", output, re.M)
    return {
        "category": (
            "none"
            if report.get("status") == "qa_passed"
            else "test_failure"
            if report.get("exit_code") == 1 and report.get("failed_tests", 0) > 0 and names
            else "execution_error"
        ),
        "failing_tests": names,
        "traceback_summary": output[-32768:],
        "error_summary": report.get("error_summary", ""),
    }


def assess_qa(state: dict) -> dict:
    feedback = failure_feedback(state["qa_results"])
    history = [
        *state.get("recovery_history", []),
        redact(
            {
                "stage": "qa",
                "attempt": state.get("qa_repair_count", 0),
                "feedback": feedback,
                "qa_results": state["qa_results"],
                "outcome": state["status"],
            }
        ),
    ]
    return {"qa_feedback": feedback, "recovery_history": history}


def begin_repair(state: dict) -> dict:
    """Checkpoint the attempt before any model call, so replay cannot evade the budget."""
    return {"qa_repair_count": state.get("qa_repair_count", 0) + 1, "status": "repairing"}


def allowed_sources(files: dict[str, str], feedback: dict) -> set[str]:
    """Only existing source named by a traceback or directly imported by failing tests."""
    relative = {str(PurePosixPath(p).relative_to("workspace/output_code")): p for p in files}
    cited = {
        str(PurePosixPath(f["file_path"]).relative_to("workspace/output_code"))
        for f in feedback.get("review_findings", [])
        if f.get("file_path") in files
    }
    cited.update(
        str(PurePosixPath(path).relative_to("workspace/output_code"))
        for path in feedback.get("human_paths", [])
        if path in files
    )
    cited.update(re.findall(r"([A-Za-z0-9_./-]+\.py):\d+", feedback["traceback_summary"]))
    for name in feedback["failing_tests"]:
        test = files.get(relative.get(name.split("::")[0], ""), "")
        for node in ast.walk(ast.parse(test)):
            if isinstance(node, ast.Import):
                modules = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                modules = [node.module, *[node.module + "." + a.name for a in node.names]]
            else:
                continue
            cited.update(module.replace(".", "/") + ".py" for module in modules)
    protected = set()
    for path, content in files.items():
        if not path.endswith(".py"):
            continue
        tree = ast.parse(content)
        if any(
            isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
            and n.name.startswith("test_")
            or isinstance(n, ast.Import)
            and any(a.name == "pytest" for a in n.names)
            or isinstance(n, ast.ImportFrom)
            and n.module == "pytest"
            for n in ast.walk(tree)
        ):
            protected.add(path)
    return {
        relative[p]
        for p in cited
        if p in relative
        and relative[p] not in protected
        and p.endswith(".py")
        and not any(part in {"tests", "test"} for part in PurePosixPath(p).parts)
        and not PurePosixPath(p).name.startswith(("test_", "conftest"))
        and not PurePosixPath(p).name.endswith("_test.py")
    }


def preserve_guards(before: str, after: str) -> None:
    """Conservative guard retention; ambiguous changes stop for human review."""

    def guards(text):
        nodes = ast.walk(ast.parse(text, feature_version=(3, 12)))
        return Counter(
            ast.dump(n.test if isinstance(n, ast.If) else n, include_attributes=False)
            for n in nodes
            if isinstance(n, (ast.If, ast.Raise, ast.Assert))
        )

    if guards(before) - guards(after):
        raise ValueError(
            "Repair cannot remove or alter existing validation guards; human review required."
        )


def repair_developer(state: dict) -> dict:
    """Consume defect or human feedback, persist a safe source repair, and checkpoint its
    outcome.
    """
    human = state.get("repair_mode") == "human"
    count = state.get("human_change_count", 0) if human else state["qa_repair_count"]
    event = {
        "stage": "human_repair" if human else "developer_repair",
        "attempt": count,
        "changed_files": [],
        "failure_category": state["qa_feedback"]["category"],
        "outcome": "failed",
    }
    try:
        if not state.get("qa_local_authorized") or not (
            count >= 1 if human else 1 <= count <= MAX_REPAIRS
        ):
            raise ValueError("Repair requires execution authorization and available budget.")
        requirements, design, plan = development_inputs(state)
        with SafeWorkspace(state["run_id"]) as workspace:
            files = workspace.snapshot()
            allowed = allowed_sources(files, state["qa_feedback"])
            if not allowed:
                raise ValueError(
                    "No affected application source identified; test changes require human approval."
                )
            context = {
                "mode": "human_repair" if human else "qa_repair",
                "requirements": requirements.model_dump(),
                "design": design.model_dump(),
                "plan": plan.model_dump(),
                "existing_files": files,
                "allowed_repair_paths": sorted(allowed),
                "qa_feedback": state["qa_feedback"],
                "review_findings": state["qa_feedback"].get("review_findings", []),
                "validation_revision": state.get("validation_revision"),
                "prior_repairs": [
                    h
                    for h in state.get("recovery_history", [])
                    if h.get("stage") in {"developer_repair", "human_repair"}
                ],
                "task_id": f"human-repair-{count}" if human else f"qa-repair-{count}",
            }
            fingerprint = hashlib.sha256(
                json.dumps(
                    {"feedback": state["qa_feedback"], "attempt": count}, sort_keys=True
                ).encode()
            ).hexdigest()
            output = workspace.receipt(context["task_id"], fingerprint)
            if output is None:
                output = generate_task(context)
                if (
                    output.task_id != context["task_id"]
                    or output.completion_status != "completed"
                    or not output.files
                ):
                    raise ValueError("Repair returned invalid task result.")
                if not {f.path for f in output.files} <= allowed:
                    raise ValueError(
                        "Repair attempted an unrelated file or test change; human approval required."
                    )
                if all(files[f.path] == f.content for f in output.files):
                    raise ValueError("Repair made no source changes.")
                for file in output.files:
                    preserve_guards(files[file.path], file.content)
                candidate = {**files, **{f.path: f.content for f in output.files}}
                if validate_dependencies(candidate, final=True):
                    raise ValueError("Repair failed dependency validation.")
                workspace.persist(output, fingerprint)
            event.update(
                outcome="applied",
                changed_files=[f.path for f in output.files],
                implementation_summary=output.implementation_summary,
            )
        status, error = "development_complete", None
    except (ValueError, TypeError, OSError, SyntaxError, KeyError) as exc:
        known = {
            "No affected application source identified; test changes require human approval.",
            "Repair attempted an unrelated file or test change; human approval required.",
            "Repair cannot remove or alter existing validation guards; human review required.",
            "Repair returned invalid task result.",
            "Repair failed dependency validation.",
            "Repair made no source changes.",
            "OpenAI developer request failed.",
        }
        event["details"] = (
            str(exc)
            if str(exc) in known
            else "Generation, syntax, contract, or safe persistence validation failed."
        )
        error = "Repair rejected or unavailable. Review recovery history; files were not approved for QA."
        event["error"] = error
        status = "qa_failed"
    return {
        "status": status,
        "error": error,
        "qa_repair_applied": status == "development_complete",
        "recovery_history": [*state.get("recovery_history", []), redact(event)],
    }
