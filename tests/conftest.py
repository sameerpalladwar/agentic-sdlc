from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

import agents.design as design
import agents.developer as developer
import agents.planning as planning
import agents.requirements as agent
import agents.reviewer as reviewer


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
    monkeypatch.setattr(developer, "load_dotenv", lambda *args: None)
    monkeypatch.setattr(developer, "OpenAI", MagicMock(return_value=client))
    monkeypatch.setattr(reviewer, "load_dotenv", lambda *args: None)
    monkeypatch.setattr(reviewer, "OpenAI", MagicMock(return_value=client))
    return client


@pytest.fixture
def payload():
    return {
        "summary": "Build a URL shortener service",
        "user_stories": [
            {
                "id": "US-001",
                "title": "Shorten a URL",
                "as_a": "user",
                "i_want": "to shorten a URL",
                "so_that": "I can share a short link",
                "priority": "must",
                "acceptance_criteria": [
                    {
                        "given": "a valid URL",
                        "when": "I submit it",
                        "then": "a short link is returned",
                    }
                ],
            }
        ],
        "non_functional_requirements": [],
        "assumptions": [],
        "open_questions": [],
        "requires_human_clarification": False,
    }


@pytest.fixture
def llm_response(isolated_llm, payload):
    isolated_llm.responses.parse.return_value = SimpleNamespace(
        status="completed",
        output_parsed=payload,
    )
    return isolated_llm.responses.parse


@pytest.fixture
def design_payload():
    return {
        "architecture_summary": "A FastAPI service stores URL mappings in SQLite.",
        "technology_stack": [
            {"name": name, "purpose": purpose, "rationale": "Simple MVP default"}
            for name, purpose in [
                ("FastAPI", "HTTP API"),
                ("SQLite", "Persistence"),
                ("pytest", "Tests"),
            ]
        ],
        "components": [
            {
                "name": "URL API",
                "responsibilities": ["Validate and shorten URLs"],
                "dependencies": ["SQLite"],
            }
        ],
        "api_endpoints": [
            {
                "method": "POST",
                "path": "/urls",
                "request": "JSON url: valid HTTP URL",
                "response": "201 JSON short_url: string",
                "errors": ["422 invalid URL"],
            }
        ],
        "data_models": [
            {
                "name": "URLMapping",
                "fields": [{"name": "code", "type": "text", "constraints": "unique primary key"}],
                "relationships": [],
            }
        ],
        "non_functional_design": [],
        "implementation_plan": ["Implement API and persistence", "Add pytest acceptance tests"],
        "assumptions": ["FastAPI, SQLite and pytest are defaults"],
        "risks": ["SQLite write concurrency is limited"],
        "story_traceability": [
            {
                "story_id": "US-001",
                "design_elements": ["URL API POST /urls"],
                "acceptance_criteria_coverage": ["Test valid URL returns short_url"],
            }
        ],
    }


def approved_review(context):
    from models.review import CodeReviewResult

    return CodeReviewResult(
        status="approved",
        summary="Mock independent review",
        findings=[],
        blocking_findings_count=0,
        reviewed_files=list(context["files"]),
        reviewed_revision=context["revision"],
    )


@pytest.fixture
def ready(tmp_path, monkeypatch, payload, design_payload):
    """Trusted completed-development state shared by QA, Review, and delivery tests."""
    from models.developer import DeveloperOutput
    from orchestrator import file_writer
    from orchestrator.file_writer import SafeWorkspace
    from tests.test_qa import completed_state
    from tests.test_qa_recovery import BAD, RUN, TEST

    monkeypatch.setattr(file_writer, "OUTPUT_ROOT", tmp_path / "output")

    monkeypatch.setattr("agents.reviewer.generate_review", approved_review)
    state = completed_state(payload, design_payload, RUN)
    with SafeWorkspace(RUN) as workspace:
        output = DeveloperOutput(
            task_id="trusted-fixture",
            files=[
                {"path": "workspace/output_code/application.py", "content": BAD},
                {"path": "workspace/output_code/test_app.py", "content": TEST},
            ],
            implementation_summary="Trusted handwritten fixture",
            acceptance_check_mapping=[
                {"check": "value returns one", "implementation": "Handwritten test"}
            ],
            assumptions=[],
            warnings=[],
            completion_status="completed",
        )
        workspace.persist(output, "trusted-fixture")
    state.update(qa_local_authorized=True, through="qa", recovery_history=[])
    return state
