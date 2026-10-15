from orchestrator.state import WorkflowState


def requirements_agent(state: WorkflowState) -> dict:
    """Understand the incoming engineering requirement."""
    requirement = state["requirement"].strip()
    if not requirement:
        return {"requirements_summary": "", "status": "failed"}
    return {
        "requirements_summary": f"Engineering requirement: {requirement}",
        "status": "requirements_complete",
    }
