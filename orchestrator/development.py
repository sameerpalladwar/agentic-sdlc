"""Schedule dependency-ready tasks and persist generated files with replay-safe receipts."""

import hashlib
import json

from langgraph.types import interrupt

from agents.developer import generate_task
from models.design import DesignOutput
from models.developer import DeveloperOutput
from models.planning import ExecutionPlan
from models.requirements import RequirementsOutput
from orchestrator.dependencies import validate_dependencies
from orchestrator.file_writer import SafeWorkspace
from orchestrator.scenarios import project_context
from orchestrator.state import WorkflowState
from orchestrator.task_plan import InvalidPlanError, calculate_readiness, validate_execution_plan

MAX_TASK_ATTEMPTS = 3


def development_inputs(
    state: WorkflowState,
) -> tuple[RequirementsOutput, DesignOutput, ExecutionPlan]:
    requirements = RequirementsOutput.model_validate(state.get("requirements"))
    design = DesignOutput.model_validate(state.get("design"))
    plan = ExecutionPlan.model_validate(state.get("plan"))
    try:
        # The validator resets only its returned copy; retain persisted lifecycle statuses.
        validate_execution_plan(plan, requirements)
    except ValueError as error:
        raise InvalidPlanError(str(error)) from error
    if requirements.requires_human_clarification:
        raise ValueError("Requirements still need clarification.")
    return requirements, design, plan


def prepare_development(state: WorkflowState) -> dict:
    """Validate approved state and initialize a bounded generation budget without executing
    generated code.
    """
    try:
        if state["status"] != "planning_complete":
            raise ValueError("Development requires completed planning.")
        _, _, plan = development_inputs(state)
        if any(t.status not in {"ready", "blocked", "pending"} for t in plan.tasks):
            raise ValueError("Fresh development requires an unexecuted valid plan.")
        budget = state.get("development_budget", 20)
        if not 1 <= budget <= 50:
            raise ValueError("Development budget must be between 1 and 50.")
        if state.get("project_mode") == "brownfield":
            seed_repository(state)
        return {
            "status": "developing",
            "development_results": [],
            "development_used": 0,
            "active_task": None,
            "error": None,
        }
    except InvalidPlanError as error:
        return invalid_plan_result(error)
    except (ValueError, TypeError, OSError, SyntaxError):
        return {
            "status": "failed",
            "error": "Development requires a valid completed plan, design, and clear requirements.",
        }


def select_task(state: WorkflowState) -> dict:
    """Choose a dependency-ready task or stop; validate final dependencies before declaring
    completion.
    """
    try:
        _, _, plan = development_inputs(state)
        with SafeWorkspace(state["run_id"]) as workspace:
            files = workspace.snapshot()
        missing_artifacts = [
            {
                "code": "missing_task_artifact",
                "task_id": task.task_id,
                "field": path,
                "message": f"Task {task.task_id} is completed but its artifact {path} is not persisted.",
            }
            for task in plan.tasks
            if task.status == "completed"
            for path in task.expected_artifacts
            if path not in files
        ]
        if missing_artifacts:
            return {
                "status": "failed",
                "active_task": None,
                "development_issues": missing_artifacts,
                "error": "; ".join(issue["message"] for issue in missing_artifacts),
            }
        plan.ready_task_ids, plan.blocked_task_ids = calculate_readiness(plan.tasks)
        if all(t.status == "completed" for t in plan.tasks):
            issues = validate_dependencies(files, final=True)
            if issues:
                return {
                    "status": "failed",
                    "active_task": None,
                    "development_issues": issues,
                    "plan": plan.model_dump(),
                    "error": "All tasks completed, but generated application package/runtime validation failed: "
                    + "; ".join(issue["message"] for issue in issues),
                }
            return {
                "status": "development_complete",
                "active_task": None,
                "plan": plan.model_dump(),
                "development_issues": [],
                "error": None,
            }
        failed = any(t.status == "failed" for t in plan.tasks)
        diagnostics = [*state.get("development_issues", []), *task_issues(plan)]
        if (
            failed
            or state.get("development_used", 0) >= state.get("development_budget", 20)
            or not plan.ready_task_ids
        ):
            return {
                "status": "development_paused",
                "active_task": None,
                "plan": plan.model_dump(),
                "development_issues": diagnostics,
                "error": (
                    "Development paused: " + "; ".join(issue["message"] for issue in diagnostics)
                    if failed or not plan.ready_task_ids
                    else f"Development generation budget exhausted ({state.get('development_used', 0)}/{state.get('development_budget', 20)}); ready task IDs: {plan.ready_task_ids}."
                ),
            }
        task = next(t for t in plan.tasks if t.task_id == plan.ready_task_ids[0])
        task.status = "in_progress"
        plan.ready_task_ids.remove(task.task_id)
        return {
            "active_task": task.task_id,
            "plan": plan.model_dump(),
            "status": "developing",
            "development_issues": [],
            "error": None,
        }
    except InvalidPlanError as error:
        return invalid_plan_result(error)
    except (ValueError, TypeError, OSError):
        return {
            "status": "failed",
            "error": "Development input or managed workspace validation failed.",
        }


def invalid_plan_result(error: InvalidPlanError) -> dict:
    return {
        "status": "failed",
        "active_task": None,
        "development_issues": [
            {"code": "invalid_task_plan", "field": "plan", "message": str(error)}
        ],
        "error": f"Task plan validation failed before scheduling: {error}",
    }


def task_issues(plan: ExecutionPlan) -> list[dict]:
    """Keep lifecycle failures distinct from missing third-party package declarations."""
    statuses = {task.task_id: task.status for task in plan.tasks}
    issues = []
    for task in plan.tasks:
        unmet = [
            dependency for dependency in task.dependencies if statuses[dependency] != "completed"
        ]
        if task.status == "failed" or unmet:
            dependency_statuses = {dependency: statuses[dependency] for dependency in unmet}
            issues.append(
                {
                    "code": "task_failed" if task.status == "failed" else "unmet_task_dependencies",
                    "field": "plan.tasks",
                    "task_id": task.task_id,
                    "status": task.status,
                    "unmet_dependency_ids": unmet,
                    "dependency_statuses": dependency_statuses,
                    "message": f"Task {task.task_id} status={task.status}; unmet dependencies={dependency_statuses}.",
                }
            )
    return issues


def developer_agent(state: WorkflowState) -> dict:
    """Generate and persist a single task. Each return is a LangGraph checkpoint."""
    results = list(state.get("development_results", []))
    plan = ExecutionPlan.model_validate(state["plan"])
    task = next(t for t in plan.tasks if t.task_id == state["active_task"])
    attempt = 1 + sum(r["task_id"] == task.task_id for r in results)
    record = {
        "task_id": task.task_id,
        "attempt": attempt,
        "status": "failed",
        "generated_files": [],
        "result": None,
        "error": None,
        "runtime_checks_executed": False,
    }
    try:
        requirements, design, _ = development_inputs(state)
        if attempt > MAX_TASK_ATTEMPTS:
            raise ValueError("Task attempt limit reached.")
        completed = {t.task_id for t in plan.tasks if t.status == "completed"}
        if task.status != "in_progress" or not set(task.dependencies) <= completed:
            raise ValueError("Task is not ready: dependencies must be completed.")
        context = {
            "requirements": requirements.model_dump(),
            "design": design.model_dump(),
            "plan": plan.model_dump(),
            "task": task.model_dump(),
        }
        context.update(project_context(state, include_files=False))
        # Fingerprint stable inputs, excluding evolving task statuses and file contents.
        fingerprint = hashlib.sha256(
            json.dumps(
                {
                    "requirements": context["requirements"],
                    "design": context["design"],
                    "task": {**task.model_dump(), "status": "pending"},
                },
                sort_keys=True,
            ).encode()
        ).hexdigest()
        with SafeWorkspace(state["run_id"]) as workspace:
            output = workspace.receipt(task.task_id, fingerprint)
            if output is None:
                context["existing_files"] = workspace.snapshot()
                context["previous_attempts"] = [r for r in results if r["task_id"] == task.task_id]
                output = generate_task(context)
                record["result"] = output.model_dump()
                if output.task_id != task.task_id:
                    raise ValueError("Developer output has the wrong task ID.")
                if output.completion_status != "completed":
                    raise ValueError("Developer could not complete the task.")
                if {f.path for f in output.files} != set(task.expected_artifacts):
                    raise ValueError(
                        "Generated files must exactly match the task's declared artifacts."
                    )
                if [m.check for m in output.acceptance_check_mapping] != task.acceptance_checks:
                    raise ValueError("Developer output must map every acceptance check in order.")
                candidate = {**workspace.snapshot(), **{f.path: f.content for f in output.files}}
                final_task = all(
                    other.task_id == task.task_id or other.status == "completed"
                    for other in plan.tasks
                )
                issues = validate_dependencies(candidate, final=final_task)
                if issues:
                    record["issues"] = [
                        {
                            **issue,
                            "task_id": task.task_id,
                            "validation_phase": "application_dependencies",
                        }
                        for issue in issues
                    ]
                    raise ValueError("Dependency validation failed.")
                workspace.persist(output, fingerprint)
            record["generated_files"] = [str(workspace.target(f.path)) for f in output.files]
            record["result"] = output.model_dump()
        task.status = "completed"
        record["status"] = "completed"
    except (ValueError, TypeError, OSError, SyntaxError) as exc:
        safe_reasons = {
            "Dependency validation failed.",
            "Task attempt limit reached.",
            "Task is not ready: dependencies must be completed.",
            "Developer output has the wrong task ID.",
            "Developer could not complete the task.",
            "Generated files must exactly match the task's declared artifacts.",
            "Developer output must map every acceptance check in order.",
            "OpenAI developer request failed.",
            "The model returned invalid developer output.",
        }
        task.status = "failed"
        record["error"] = (
            str(exc)
            if str(exc) in safe_reasons
            else "Task generation, validation, or safe persistence failed; no runtime checks were executed."
        )
    results.append(record)
    plan.ready_task_ids, plan.blocked_task_ids = calculate_readiness(plan.tasks)
    return {
        "plan": plan.model_dump(),
        "development_results": results,
        "development_used": state.get("development_used", 0) + 1,
        "development_issues": record.get("issues", []),
        "active_task": None,
    }


def development_gate(state: WorkflowState) -> dict:
    """Interrupt for explicit retry/rejection and preserve per-task attempt limits across
    resumes.
    """
    decision = interrupt(
        {
            "kind": "development",
            "error": state.get("error"),
            "instructions": "Retry development with --retry-development or reject with --reject.",
        }
    )
    if not isinstance(decision, dict):
        raise ValueError("Development resume requires retry or rejection.")
    if decision.get("reject") is True:
        return {"status": "rejected", "error": None}
    if decision.get("retry_development") is not True:
        raise ValueError("Use --retry-development for this pause.")
    plan = ExecutionPlan.model_validate(state["plan"])
    for task in plan.tasks:
        if task.status == "failed":
            count = sum(r["task_id"] == task.task_id for r in state.get("development_results", []))
            if count >= MAX_TASK_ATTEMPTS:
                return {
                    "status": "failed",
                    "error": "Task retry limit reached; start a new run after reviewing results.",
                }
            task.status = "pending"
    return {"plan": plan.model_dump(), "status": "developing", "development_used": 0, "error": None}


def seed_repository(state: WorkflowState) -> None:
    """Copy only the analyzed snapshot into managed run output; never write to its original root."""
    files = state.get("repository_files", {})
    fingerprint = hashlib.sha256(json.dumps(files, sort_keys=True).encode()).hexdigest()
    if not files or fingerprint != (state.get("repository_analysis") or {}).get("revision"):
        raise ValueError("Brownfield source snapshot is missing or changed.")
    with SafeWorkspace(state["run_id"]) as workspace:
        if workspace.receipt("repository-baseline", fingerprint) is not None:
            return
        output = DeveloperOutput(
            task_id="repository-baseline",
            files=[{"path": path, "content": content} for path, content in files.items()],
            implementation_summary="Copy read-only repository baseline into this run's output workspace.",
            acceptance_check_mapping=[
                {
                    "check": "Preserve analyzed source",
                    "implementation": "Managed snapshot copy; no source execution.",
                }
            ],
            assumptions=[],
            warnings=["Baseline code has not been executed or approved for local QA."],
            completion_status="completed",
        )
        workspace.persist(output, fingerprint)
