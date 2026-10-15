"""Generate one structured task or repair response without executing model commands."""

import json
import os

from dotenv import load_dotenv
from openai import OpenAI, OpenAIError

from agents.prompt_loader import DEVELOPER_PROMPT, load_prompt
from agents.requirements import ENV_PATH
from models.developer import DeveloperOutput


def generate_task(context: dict) -> DeveloperOutput:
    """One bounded LLM generation; no tools or command execution are offered."""
    load_dotenv(ENV_PATH)
    key = os.getenv("OPENAI_API_KEY", "").strip()
    model = os.getenv("OPENAI_MODEL", "").strip()
    if not key or not model:
        raise ValueError("Set OPENAI_API_KEY and OPENAI_MODEL before development.")
    try:
        prompt = load_prompt(DEVELOPER_PROMPT)
    except (OSError, ValueError):
        raise ValueError("Unable to load the developer prompt.") from None
    try:
        with OpenAI(api_key=key, timeout=90.0, max_retries=0) as client:
            response = client.responses.parse(
                model=model,
                text_format=DeveloperOutput,
                input=[
                    {"role": "system", "content": prompt},
                    {"role": "user", "content": json.dumps(context, ensure_ascii=False)},
                ],
            )
        if response.status != "completed" or response.output_parsed is None:
            raise ValueError("The model returned no complete developer output.")
        return DeveloperOutput.model_validate(response.output_parsed)
    except OpenAIError:
        raise ValueError("OpenAI developer request failed.") from None
    except (TypeError, ValueError):
        raise ValueError("The model returned invalid developer output.") from None
