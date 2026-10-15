"""Small deterministic projections of node outputs; never summarize using an LLM."""

from pathlib import Path


def task_label(state: dict) -> str:
    tasks = (state.get("plan") or {}).get("tasks") or []
    for index, task in enumerate(tasks, 1):
        if task.get("task_id") == state.get("active_task"):
            return f"[{index}/{len(tasks)}] {task['task_id']} {(task.get('title') or '')[:100]}"
    return str(state.get("active_task") or "Task identity unavailable")


def approval_summary(state: dict) -> str:
    request = state.get("approval_request") or {}
    revision = request.get("revision") or state.get("validation_revision")
    if not revision or not state.get("run_id"):
        return "Approval request not available"
    return f"revision={revision}; run={state['run_id']}; python run.py decide --run-id {state['run_id']} --revision {revision} --decision approve"


def error_details(stage: str, state: dict) -> tuple[str, str]:
    if stage.startswith("planning"):
        issues = state.get("planning_validation_issues") or []
    elif stage.startswith("design"):
        issues = (
            state.get("design_validation_issues") or state.get("design_generation_issues") or []
        )
    elif stage.startswith("development") or stage == "developer":
        issues = state.get("development_issues") or []
    else:
        issues = []
    record = (state.get("development_results") or [{}])[-1] if stage == "developer" else {}
    issues = record.get("issues") or issues
    qa = state.get("qa_result") or state.get("qa_results") or {}
    review = state.get("review_result") or {}
    category = str(issues[0].get("code", "validation_error")) if issues else "stage_failure"
    reason = state.get("error") or state.get("failure_reason") or record.get("error")
    if issues:
        reason = "; ".join(issue.get("message", "Validation issue") for issue in issues[:3])
    if stage == "qa":
        reason = qa.get("error_summary") or reason
        category = qa.get("status", category)
        names = [
            line for line in qa.get("test_output", "").splitlines() if line.startswith("FAILED ")
        ]
        if names:
            reason = "; ".join(names[:3])
    elif stage == "review":
        category = review.get("status", category)
        # Findings are evidence-bearing data; avoid logging their source excerpts.
        reason = f"{review.get('blocking_findings_count', 0)} blocking findings; inspect review_results.json"
    elif stage == "planning" and not issues and reason:
        category = (
            "invalid_task_plan" if str(reason).startswith("Plan validation failed:") else category
        )
    if not reason:
        reason = "No diagnostic reason was provided; inspect execution.log"
    return category, str(reason)


def _stage_summary(stage: str, before: dict, result: dict | None = None) -> str | None:
    """Use only known structured fields; absent output must not imply a successful result."""
    state = {**before, **(result or {})}
    if result is None:
        return task_label(before) if stage == "developer" else None
    if stage == "project_routing":
        mode = state.get("project_mode")
        return f"mode={mode or 'unresolved'}; repository={'provided' if state.get('repository_path') else 'not supplied'}"
    if stage == "repository_analysis":
        analysis = state.get("repository_analysis") or {}
        return f"{len((analysis.get('files') or []))} files inspected; repository={analysis.get('repository_path', 'unavailable')}"
    if stage == "requirements":
        requirements = state.get("requirements") or {}
        if not requirements:
            return "No structured requirements available"
        stories = requirements.get("user_stories") or []
        criteria = sum(len((story.get("acceptance_criteria") or [])) for story in stories)
        story_word = "story" if len(stories) == 1 else "stories"
        criterion_word = "criterion" if criteria == 1 else "criteria"
        questions = len(requirements.get("open_questions") or [])
        return f"{len(stories)} user {story_word}, {criteria} acceptance {criterion_word}, {questions} open questions"
    if stage in {"design", "design_validator"}:
        design = state.get("design") or state.get("design_candidate") or {}
        status = (
            state.get("design_validation_status")
            if stage == "design_validator"
            else "pending validation"
        )
        return f"{len((design.get('components') or []))} components, {len((design.get('api_endpoints') or []))} API endpoints; validation={status or 'pending'}; attempt={state.get('design_retry_count', 0) + 1}"
    if stage in {"design_retry", "planning_retry"}:
        kind = "design" if stage == "design_retry" else "planning"
        return f"correction retry={state.get(kind + '_retry_count', 0)}; issues={len(state.get(kind + '_validation_issues') or [])}"
    if stage == "planning":
        plan = state.get("plan") or {}
        if not plan:
            return "No validated task DAG available"
        tasks = plan.get("tasks") or []
        return f"{len(tasks)} tasks, {sum(len((task.get('dependencies') or [])) for task in tasks)} dependencies, DAG valid; {len((plan.get('blocked_task_ids') or []))} blocked; attempt={state.get('planning_retry_count', 0) + 1}"
    if stage == "developer":
        records = state.get("development_results") or []
        record = records[-1] if records else {}
        files = len(record.get("generated_files") or [])
        revision = state.get("validation_revision") or "not yet frozen"
        return f"{task_label(before)}; {files} files persisted; revision={revision}"
    if stage in {"repair_begin", "developer_repair", "human_repair"}:
        history = state.get("recovery_history") or []
        changes = history[-1].get("changed_files", []) if history else []
        return f"status={state.get('status', 'unreported')}; {len(changes or [])} files changed; repair_attempt={state.get('qa_repair_count', 0)}; revision={state.get('validation_revision', 'not yet frozen')}"
    if stage == "qa":
        qa = state.get("qa_result") or state.get("qa_results") or {}
        if not qa:
            return "No QA result available"
        command = qa.get("command") or (
            "pytest not executed"
            if qa.get("status") == "qa_blocked" and qa.get("exit_code") is None
            else "command not recorded"
        )
        if not all(
            key in qa for key in ("total_tests", "passed_tests", "failed_tests", "skipped_tests")
        ):
            return f"{qa.get('status', 'unreported')}; test counts not reported; command={command}; exit={qa.get('exit_code')}"
        return f"{qa.get('total_tests', 0)} tests: {qa.get('passed_tests', 0)} passed, {qa.get('failed_tests', 0)} failed, {qa.get('skipped_tests', 0)} skipped; command={command}; exit={qa.get('exit_code')}"
    if stage == "review":
        review = state.get("review_result") or {}
        if not review:
            return "No Review result available"
        blocking = review.get("blocking_findings_count") or 0
        return f"{review.get('status', 'unavailable')}; {blocking} blocking, {max(0, len(review.get('findings', []) or []) - blocking)} nonblocking findings; revision={review.get('reviewed_revision', 'unavailable')}"
    if stage in {"validation_gate", "validation_finish"}:
        qa = state.get("qa_result") or {}
        review = state.get("review_result") or {}
        revision = state.get("validation_revision")
        matching = bool(
            revision
            and qa.get("revision") == review.get("reviewed_revision") == revision
            and qa.get("retry_attempt")
            == review.get("retry_attempt")
            == state.get("qa_repair_count", 0)
        )
        return f"QA={qa.get('status', 'missing')}, Review={review.get('status', 'missing')}; revision={'consistent' if matching else 'missing or mismatched'}; repair_attempt={state.get('qa_repair_count', 0)}"
    if stage == "validation_snapshot":
        return f"revision={state.get('validation_revision') or 'unavailable'}; {len(state.get('validation_files') or {})} files frozen"
    if stage in {"approval_prepare", "human_approval"}:
        if state.get("status") != "awaiting_approval":
            return f"decision={state.get('status', 'unavailable')}; revision={state.get('validation_revision', 'unavailable')}"
        return approval_summary(state)
    if stage == "delivery":
        manifest = state.get("final_manifest") or {}
        directory = state.get("run_directory") or f"workspace/runs/{state.get('run_id', 'unknown')}"
        return f"status={state.get('status')}; approved_revision={manifest.get('approved_revision', 'unavailable')}; manifest={Path(directory) / 'final_manifest.json'}"
    if stage in {"project_input", "clarification", "development_gate"}:
        return f"run={state.get('run_id', 'unknown')}; status={state.get('status', 'unknown')}"
    return None


def stage_summary(stage: str, before: dict, result: dict | None = None) -> str | None:
    """Malformed optional metrics must never change workflow routing or execution."""
    try:
        return _stage_summary(stage, before, result)
    except (AttributeError, KeyError, TypeError, ValueError):
        return "Structured metrics unavailable; inspect the stage artifact"
