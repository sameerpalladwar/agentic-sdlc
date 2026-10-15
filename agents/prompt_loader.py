from pathlib import Path


REQUIREMENTS_PROMPT = Path(__file__).resolve().parents[1] / "prompts" / "requirements.md"

DESIGN_PROMPT = REQUIREMENTS_PROMPT.with_name("design.md")


def load_prompt(path: Path = REQUIREMENTS_PROMPT) -> str:
    """Load agent instructions independently of the current directory."""
    prompt = path.read_text(encoding="utf-8").strip()
    if not prompt:
        raise ValueError("The agent prompt is empty.")
    return prompt
