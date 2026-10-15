from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

import agents.requirements as agent


@pytest.fixture(autouse=True)
def isolated_llm(monkeypatch):
    """Never load local secrets or make network calls, even in failing tests."""
    monkeypatch.setattr(agent, "load_dotenv", lambda *args: None)
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("OPENAI_MODEL", "test-model")
    client = MagicMock()
    client.__enter__.return_value = client
    monkeypatch.setattr(agent, "OpenAI", MagicMock(return_value=client))
    return client


@pytest.fixture
def payload():
    return {
        "summary": "Build a URL shortener service",
        "user_stories": [{
            "id": "US-001", "title": "Shorten a URL", "as_a": "user",
            "i_want": "to shorten a URL", "so_that": "I can share a short link",
            "priority": "must", "acceptance_criteria": [{
                "given": "a valid URL", "when": "I submit it",
                "then": "a short link is returned",
            }],
        }],
        "non_functional_requirements": [], "assumptions": [],
        "open_questions": [], "requires_human_clarification": False,
    }


@pytest.fixture
def llm_response(isolated_llm, payload):
    isolated_llm.responses.parse.return_value = SimpleNamespace(
        status="completed", output_parsed=payload,
    )
    return isolated_llm.responses.parse
