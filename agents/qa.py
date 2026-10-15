"""Recorded pytest outcomes; orchestration owns review and repair decisions."""

from models.design import DesignOutput
from models.developer import DeveloperOutput
from models.planning import ExecutionPlan
from models.qa import AcceptanceCriterionResult, QAResult
from models.requirements import RequirementsOutput
from orchestrator.file_writer import SafeWorkspace
from orchestrator.qa_runner import generated_directory, run_tests
from orchestrator.state import WorkflowState
from orchestrator.task_plan import validate_execution_plan


def qa_agent(state: WorkflowState) -> dict:
    """Validate completed development state and return actual pytest results; execution
    requires opt-in.
    """
    requirements = None
    try:
        requirements = RequirementsOutput.model_validate(state.get("requirements"))
        DesignOutput.model_validate(state.get("design"))
        plan = ExecutionPlan.model_validate(state.get("plan"))
        validate_execution_plan(plan, requirements)
        if (
            state.get("status") not in {"development_complete", "validating"}
            or requirements.requires_human_clarification
        ):
            raise ValueError("QA requires completed development and clear requirements.")
        latest = {item["task_id"]: item for item in state.get("development_results", [])}
        for task in plan.tasks:
            record = latest.get(task.task_id, {})
            output = DeveloperOutput.model_validate(record.get("result"))
            if (
                task.status != "completed"
                or record.get("status") != "completed"
                or output.completion_status != "completed"
                or output.task_id != task.task_id
            ):
                raise ValueError("All implementation tasks must have completed developer results.")
        directory = generated_directory(state["run_id"], state.get("application_dir"))
        if state.get("validation_revision"):
            from orchestrator.revision import frozen_files

            files = frozen_files(state)
        else:
            with SafeWorkspace(state["run_id"]) as workspace:
                if workspace.manifest.get("pending"):
                    raise ValueError("Generated files still have unfinished writes.")
                files = workspace.snapshot()
        report = run_tests(
            state["run_id"],
            files,
            local_authorized=state.get("qa_local_authorized", False),
            application_dir=str(directory),
        )
    except (ValueError, TypeError, KeyError, OSError):
        report = QAResult(
            status="qa_blocked",
            error_summary="QA input or managed workspace validation failed. Check completed development and file hashes.",
        )
    if requirements:
        report.acceptance_criteria_results = [
            AcceptanceCriterionResult(story_id=story.id, criterion_index=index)
            for story in requirements.user_stories
            if story.priority != "wont"
            for index, _ in enumerate(story.acceptance_criteria, 1)
        ]
    return {
        "qa_results": report.model_dump(mode="json"),
        "status": report.status,
        "error": report.error_summary or None,
    }


def qa_validation_branch(state: WorkflowState) -> dict:
    """Parallel node owns only qa_result; the gate owns shared lifecycle fields."""
    result = qa_agent(state)["qa_results"]
    result.update(
        revision=state.get("validation_revision"), retry_attempt=state.get("qa_repair_count", 0)
    )
    return {"qa_result": result}
