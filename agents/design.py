import json
import os

from dotenv import load_dotenv
from openai import OpenAI, OpenAIError
from pydantic import ValidationError

from agents.prompt_loader import DESIGN_PROMPT, load_prompt
from agents.requirements import ENV_PATH
from models.design import DesignOutput
from models.requirements import RequirementsOutput
from orchestrator.state import WorkflowState


def design_agent(state: WorkflowState) -> dict:
    """Design only validated, clear requirements passed through the workflow gate."""
    failure = {"design": None, "status": "failed", "error": None}
    try:
        requirements = RequirementsOutput.model_validate(state.get("requirements"))
    except (ValidationError, TypeError, ValueError):
        return {**failure, "error": "Design requires valid structured requirements."}
    if state["status"] != "requirements_complete" or requirements.requires_human_clarification:
        return {**failure, "error": "Design requires completed requirements without blocking clarification."}

    load_dotenv(ENV_PATH)
    api_key = os.getenv("OPENAI_API_KEY", "").strip()
    model = os.getenv("OPENAI_MODEL", "").strip()
    if not api_key or not model:
        return {**failure, "error": "Set OPENAI_API_KEY and OPENAI_MODEL in .env or the environment."}
    try:
        prompt = load_prompt(DESIGN_PROMPT)
    except (OSError, ValueError):
        return {**failure, "error": "Unable to load the design prompt."}

    # Include original constraints: the requirements summary may omit technologies.
    context = {"requirements": requirements.model_dump(mode="json"),
               "original_requirement": state["requirement"],
               "clarifications": state.get("clarifications", [])}
    try:
        with OpenAI(api_key=api_key, timeout=60.0, max_retries=2) as client:
            response = client.responses.parse(
                model=model,
                input=[{"role": "system", "content": prompt},
                       {"role": "user", "content": json.dumps(context, ensure_ascii=False)}],
                text_format=DesignOutput,
            )
        if response.status != "completed" or response.output_parsed is None:
            return {**failure, "error": "The model returned no complete structured design (possibly a refusal)."}
        output = DesignOutput.model_validate(response.output_parsed)
        entries = {entry.story_id: entry for entry in output.story_traceability}
        if len(entries) != len(output.story_traceability) or set(entries) != {s.id for s in requirements.user_stories}:
            raise ValueError("Design must trace each story exactly once.")
        for story in requirements.user_stories:
            if len(entries[story.id].acceptance_criteria_coverage) != len(story.acceptance_criteria):
                raise ValueError("Design must cover every acceptance criterion.")
    except (ValidationError, ValueError, TypeError):
        return {**failure, "error": "The model returned invalid design or story traceability."}
    except OpenAIError:
        return {**failure, "error": "OpenAI design request failed. Check credentials, model access, and connectivity."}
    return {"design": output.model_dump(mode="json"), "status": "design_complete", "error": None}
