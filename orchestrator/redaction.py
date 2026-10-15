"""Shared redaction for allowlisted log fields and one-line terminal summaries."""

import os
import re


def redact_text(text: str) -> str:
    """Redact before JSON encoding so quoted/newline-containing secrets cannot escape masking."""
    for name, value in os.environ.items():
        if len(value) >= 4 and re.search(r"KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL|AUTH", name, re.I):
            text = text.replace(value, "[REDACTED]")
    text = re.sub(r"sk-[A-Za-z0-9_-]{12,}|\bBearer\s+\S+", "[REDACTED]", text)
    return re.sub(
        r"(?i)(api[_-]?key|password|authorization|token)\s*[:=]\s*\S+", r"\1=[REDACTED]", text
    )


def terminal_text(text: str, limit: int = 600) -> str:
    """Redact before truncation, then remove terminal controls and collapse whitespace."""
    text = redact_text(str(text))
    text = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", text)
    text = re.sub(r"[\x00-\x1f\x7f-\x9f]", " ", text)
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def redact_data(value):
    """Redact leaf strings before JSON escaping, including multiline environment secrets."""
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, list):
        return [redact_data(item) for item in value]
    if isinstance(value, dict):
        return {key: redact_data(item) for key, item in value.items()}
    return value
