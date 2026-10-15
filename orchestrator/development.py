import hashlib
import json

from langgraph.types import interrupt

from agents.developer import generate_task
from models.design import DesignOutput
from models.planning import ExecutionPlan
from models.requirements import RequirementsOutput
from orchestrator.file_writer import SafeWorkspace
from orchestrator.state import WorkflowState
from orchestrator.task_plan import calculate_readiness, validate_execution_plan


MAX_TASK_ATTEMPTS = 3


def development_inputs(state: WorkflowState) -> tuple[RequirementsOutput, DesignOutput, ExecutionPlan]:
    requirements = RequirementsOutput.model_validate(state.get("requirements"))
    design = DesignOutput.model_validate(state.get("design"))
    plan = ExecutionPlan.model_validate(state.get("plan"))
    validate_execution_plan(plan, requirements)  # Validate DAG without resetting persisted lifecycle state.
    if requirements.requires_human_clarification:
        raise ValueError("Requirements still need clarification.")
    return requirements, design, plan


def prepare_development(state: WorkflowState) -> dict:
    try:
        if state["status"] != "planning_complete":
            raise ValueError("Development requires completed planning.")
        _, _, plan = development_inputs(state)
        if any(t.status not in {"ready", "blocked", "pending"} for t in plan.tasks):
            raise ValueError("Fresh development requires an unexecuted valid plan.")
        budget = state.get("development_budget", 20)
        if not 1 <= budget <= 50:
            raise ValueError("Development budget must be between 1 and 50.")
        return {"status": "developing", "development_results": [], "development_used": 0,
                "active_task": None, "error": None}
    except (ValueError, TypeError):
        return {"status": "failed", "error": "Development requires a valid completed plan, design, and clear requirements."}


def select_task(state: WorkflowState) -> dict:
    try:
        _, _, plan = development_inputs(state)
        with SafeWorkspace(state["run_id"]) as workspace:
            workspace.snapshot()
        plan.ready_task_ids, plan.blocked_task_ids = calculate_readiness(plan.tasks)
        if all(t.status == "completed" for t in plan.tasks):
            return {"status": "development_complete", "active_task": None, "plan": plan.model_dump(), "error": None}
        failed = any(t.status == "failed" for t in plan.tasks)
        if failed or state.get("development_used", 0) >= state.get("development_budget", 20) or not plan.ready_task_ids:
            return {"status": "development_paused", "active_task": None, "plan": plan.model_dump(),
                    "error": "Development paused after a task failure, blocked prerequisites, or generation budget exhaustion."}
        task = next(t for t in plan.tasks if t.task_id == plan.ready_task_ids[0])
        task.status = "in_progress"
        plan.ready_task_ids.remove(task.task_id)
        return {"active_task": task.task_id, "plan": plan.model_dump(), "status": "developing"}
    except (ValueError, TypeError, OSError):
        return {"status": "failed", "error": "Development input or managed workspace validation failed."}


def developer_agent(state: WorkflowState) -> dict:
    """Generate and persist a single task. Each return is a LangGraph checkpoint."""
    results = list(state.get("development_results", []))
    plan = ExecutionPlan.model_validate(state["plan"])
    task = next(t for t in plan.tasks if t.task_id == state["active_task"])
    attempt = 1 + sum(r["task_id"] == task.task_id for r in results)
    record = {"task_id": task.task_id, "attempt": attempt, "status": "failed", "generated_files": [],
              "result": None, "error": None, "runtime_checks_executed": False}
    try:
        requirements, design, _ = development_inputs(state)
        if attempt > MAX_TASK_ATTEMPTS:
            raise ValueError("Task attempt limit reached.")
        completed = {t.task_id for t in plan.tasks if t.status == "completed"}
        if task.status != "in_progress" or not set(task.dependencies) <= completed:
            raise ValueError("Task is not ready: dependencies must be completed.")
        context = {"requirements": requirements.model_dump(), "design": design.model_dump(),
                   "plan": plan.model_dump(), "task": task.model_dump()}
        # Fingerprint stable inputs, excluding evolving task statuses and file contents.
        fingerprint = hashlib.sha256(json.dumps({"requirements": context["requirements"], "design": context["design"],
                                                "task": {**task.model_dump(), "status": "pending"}}, sort_keys=True).encode()).hexdigest()
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
                    raise ValueError("Generated files must exactly match the task's declared artifacts.")
                if [m.check for m in output.acceptance_check_mapping] != task.acceptance_checks:
                    raise ValueError("Developer output must map every acceptance check in order.")
                workspace.persist(output, fingerprint)
            record["generated_files"] = [str(workspace.target(f.path)) for f in output.files]
            record["result"] = output.model_dump()
        task.status = "completed"
        record["status"] = "completed"
    except (ValueError, TypeError, OSError, SyntaxError) as exc:
        safe_reasons = {
            "Task attempt limit reached.", "Task is not ready: dependencies must be completed.",
            "Developer output has the wrong task ID.", "Developer could not complete the task.",
            "Generated files must exactly match the task's declared artifacts.",
            "Developer output must map every acceptance check in order.",
            "OpenAI developer request failed.", "The model returned invalid developer output.",
        }
        task.status = "failed"
        record["error"] = str(exc) if str(exc) in safe_reasons else "Task generation, validation, or safe persistence failed; no runtime checks were executed."
    results.append(record)
    plan.ready_task_ids, plan.blocked_task_ids = calculate_readiness(plan.tasks)
    return {"plan": plan.model_dump(), "development_results": results,
            "development_used": state.get("development_used", 0) + 1, "active_task": None}


def development_gate(state: WorkflowState) -> dict:
    decision = interrupt({"kind": "development", "error": state.get("error"),
                          "instructions": "Retry development with --retry-development or reject with --reject."})
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
                return {"status": "failed", "error": "Task retry limit reached; start a new run after reviewing results."}
            task.status = "pending"
    return {"plan": plan.model_dump(), "status": "developing", "development_used": 0, "error": None}
