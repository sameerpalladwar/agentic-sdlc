"""Deterministic plan coverage, ownership, dependency validation, and readiness."""

from collections import Counter, deque

from models.planning import ExecutionPlan, ImplementationTask
from models.requirements import RequirementsOutput


class InvalidPlanError(ValueError):
    """Deterministic plan validation failed before scheduling or generation."""


def topological_sort(tasks: list[ImplementationTask]) -> list[str]:
    """Stable Kahn sort; reject invalid edges and cycles without executing tasks."""
    by_id = {task.task_id: task for task in tasks}
    if len(by_id) != len(tasks):
        raise ValueError(
            f"Task IDs must be unique; duplicate IDs: {sorted(k for k, n in Counter(t.task_id for t in tasks).items() if n > 1)}."
        )
    children: dict[str, list[str]] = {task_id: [] for task_id in by_id}
    indegree: dict[str, int] = {}
    for task in tasks:
        if len(task.dependencies) != len(set(task.dependencies)):
            raise ValueError(
                f"Task {task.task_id}: dependencies must not repeat: {task.dependencies}."
            )
        indegree[task.task_id] = len(task.dependencies)
        for dependency in task.dependencies:
            if dependency == task.task_id:
                raise ValueError(f"Task {task.task_id} cannot depend on itself.")
            if dependency not in by_id:
                raise ValueError(
                    f"Task {task.task_id}: dependency {dependency} refers to a missing task."
                )
            children[dependency].append(task.task_id)
    ready = deque(task_id for task_id, count in indegree.items() if count == 0)
    ordered: list[str] = []
    while ready:
        task_id = ready.popleft()
        ordered.append(task_id)
        for child in children[task_id]:
            indegree[child] -= 1
            if indegree[child] == 0:
                ready.append(child)
    if len(ordered) != len(tasks):
        raise ValueError(
            f"Task dependencies contain a cycle; unresolved task IDs: {sorted(k for k, n in indegree.items() if n > 0)}."
        )
    return ordered


def validate_artifact_ownership(plan: ExecutionPlan) -> None:
    owners: dict[str, set[str]] = {}
    for task in plan.tasks:
        for path in task.expected_artifacts:
            owners.setdefault(path, set()).add(task.task_id)
    shared = {path: ids for path, ids in owners.items() if len(ids) > 1}
    justifications = {item.path: item for item in plan.shared_artifacts}
    if len(justifications) != len(plan.shared_artifacts) or set(justifications) != set(shared):
        raise ValueError("Every shared artifact needs exactly one explicit justification.")
    for path, item in justifications.items():
        if len(item.task_ids) != len(set(item.task_ids)) or set(item.task_ids) != shared[path]:
            raise ValueError("Shared artifact justification must name exactly its owning tasks.")


def calculate_readiness(tasks: list[ImplementationTask]) -> tuple[list[str], list[str]]:
    """Recalculate pending/ready/blocked tasks; preserve execution lifecycle states."""
    completed = {task.task_id for task in tasks if task.status == "completed"}
    ready: list[str] = []
    blocked: list[str] = []
    for task in tasks:
        if task.status not in {"pending", "ready", "blocked"}:
            continue
        task.status = "ready" if all(dep in completed for dep in task.dependencies) else "blocked"
        (ready if task.status == "ready" else blocked).append(task.task_id)
    return ready, blocked


def validate_execution_plan(plan: ExecutionPlan, requirements: RequirementsOutput) -> ExecutionPlan:
    """Validate a fresh LLM plan and derive scheduling data on a copy."""
    plan = plan.model_copy(deep=True)
    order = topological_sort(plan.tasks)
    validate_artifact_ownership(plan)
    known = {story.id for story in requirements.user_stories}
    required = {story.id for story in requirements.user_stories if story.priority != "wont"}
    covered = {story_id for task in plan.tasks for story_id in task.related_user_story_ids}
    if not covered <= known:
        raise ValueError("Plan references unknown user stories.")
    if not required <= covered:
        raise ValueError("Plan does not cover every required user story.")
    excluded = known - required
    if covered & excluded:
        raise ValueError("Plan must not implement explicitly excluded stories.")
    by_id = {task.task_id: task for task in plan.tasks}
    plan.tasks = [by_id[task_id] for task_id in order]
    # These are unexecuted tasks, regardless of any completion claims from the model.
    for task in plan.tasks:
        task.status = "pending"
    plan.ordered_task_ids = order
    plan.ready_task_ids, plan.blocked_task_ids = calculate_readiness(plan.tasks)
    return plan
