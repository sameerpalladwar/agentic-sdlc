from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

import agents.requirements as agent
import agents.design as design
import agents.planning as planning


@pytest.fixture(autouse=True)
def isolated_llm(monkeypatch):
    """Never load local secrets or make network calls, even in failing tests."""
    monkeypatch.setattr(agent, "load_dotenv", lambda *args: None)
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("OPENAI_MODEL", "test-model")
    client = MagicMock()
    client.__enter__.return_value = client
    monkeypatch.setattr(agent, "OpenAI", MagicMock(return_value=client))
    monkeypatch.setattr(design, "load_dotenv", lambda *args: None)
    monkeypatch.setattr(design, "OpenAI", MagicMock(return_value=client))
    monkeypatch.setattr(planning, "load_dotenv", lambda *args: None)
    monkeypatch.setattr(planning, "OpenAI", MagicMock(return_value=client))
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


@pytest.fixture
def design_payload():
    return {
        "architecture_summary": "A FastAPI service stores URL mappings in SQLite.",
        "technology_stack": [{"name": name, "purpose": purpose, "rationale": "Simple MVP default"}
                             for name, purpose in [("FastAPI", "HTTP API"), ("SQLite", "Persistence"), ("pytest", "Tests")]],
        "components": [{"name": "URL API", "responsibilities": ["Validate and shorten URLs"], "dependencies": ["URL repository"]}],
        "api_endpoints": [{"method": "POST", "path": "/urls", "request": "JSON url: valid HTTP URL",
                           "response": "201 JSON short_url: string", "errors": ["422 invalid URL"]}],
        "data_models": [{"name": "URLMapping", "fields": [{"name": "code", "type": "text", "constraints": "unique primary key"}], "relationships": []}],
        "non_functional_design": [], "implementation_plan": ["Implement API and persistence", "Add pytest acceptance tests"],
        "assumptions": ["FastAPI, SQLite and pytest are defaults"], "risks": ["SQLite write concurrency is limited"],
        "story_traceability": [{"story_id": "US-001", "design_elements": ["URL API POST /urls"],
                                "acceptance_criteria_coverage": ["Test valid URL returns short_url"]}],
    }
