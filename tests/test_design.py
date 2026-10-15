import copy
import json
from types import SimpleNamespace

import httpx
import pytest
from openai import APIConnectionError
from pydantic import ValidationError

import agents.design as agent
from models.design import DesignOutput
from orchestrator.graph import build_graph
import run


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


def response(payload):
    return SimpleNamespace(status="completed", output_parsed=payload)


def state(payload):
    return {"requirement": "Shorten URLs using FastAPI and SQLite", "requirements": payload,
            "status": "requirements_complete", "through": "design"}


def test_success(isolated_llm, payload, design_payload):
    isolated_llm.responses.parse.return_value = response(design_payload)
    result = agent.design_agent(state(payload))
    assert result["status"] == "design_complete"
    assert result["design"] == design_payload
    kwargs = isolated_llm.responses.parse.call_args.kwargs
    assert kwargs["text_format"] is DesignOutput
    context = json.loads(kwargs["input"][1]["content"])
    assert context["requirements"] == payload
    assert "FastAPI and SQLite" in context["original_requirement"]
    assert kwargs["input"][0]["content"] == agent.load_prompt(agent.DESIGN_PROMPT)


@pytest.mark.parametrize("requirements", [None, {}, {"summary": "bad"}])
def test_missing_requirements(requirements, isolated_llm):
    result = agent.design_agent(state(requirements))
    assert result["status"] == "failed"
    isolated_llm.responses.parse.assert_not_called()


@pytest.mark.parametrize("status", ["paused", "failed", "rejected", "pending"])
def test_ineligible_status(status, payload, isolated_llm):
    data = state(payload)
    data["status"] = status
    assert agent.design_agent(data)["status"] == "failed"
    isolated_llm.responses.parse.assert_not_called()


def test_blocking_requirements_even_with_complete_status(payload, isolated_llm):
    payload.update(requires_human_clarification=True, open_questions=["What behavior?"])
    assert agent.design_agent(state(payload))["status"] == "failed"
    isolated_llm.responses.parse.assert_not_called()


@pytest.mark.parametrize("change", ["missing", "method", "path", "empty_components", "extra"])
def test_schema_validation(design_payload, change):
    if change == "missing":
        del design_payload["architecture_summary"]
    elif change == "method":
        design_payload["api_endpoints"][0]["method"] = "RUN"
    elif change == "path":
        design_payload["api_endpoints"][0]["path"] = "urls"
    elif change == "empty_components":
        design_payload["components"] = []
    else:
        design_payload["extra"] = True
    with pytest.raises(ValidationError):
        DesignOutput.model_validate(design_payload)


@pytest.mark.parametrize("parsed", [None, {}, "bad JSON"])
def test_invalid_llm_response(parsed, isolated_llm, payload):
    isolated_llm.responses.parse.return_value = response(parsed)
    result = agent.design_agent(state(payload))
    assert result["status"] == "failed"
    assert result["design"] is None


@pytest.mark.parametrize("change", ["unknown", "duplicate", "coverage"])
def test_traceability_validation(change, isolated_llm, payload, design_payload):
    if change == "unknown":
        design_payload["story_traceability"][0]["story_id"] = "US-999"
    elif change == "duplicate":
        design_payload["story_traceability"] *= 2
    else:
        design_payload["story_traceability"][0]["acceptance_criteria_coverage"] *= 2
    isolated_llm.responses.parse.return_value = response(design_payload)
    assert agent.design_agent(state(payload))["status"] == "failed"


def test_api_error(isolated_llm, payload):
    isolated_llm.responses.parse.side_effect = APIConnectionError(request=httpx.Request("POST", "https://api.openai.com"))
    result = agent.design_agent(state(payload))
    assert result["status"] == "failed"
    assert result["design"] is None
    assert "OpenAI design request failed" in result["error"]


def test_missing_configuration(monkeypatch, isolated_llm, payload):
    monkeypatch.delenv("OPENAI_API_KEY")
    assert agent.design_agent(state(payload))["status"] == "failed"
    isolated_llm.responses.parse.assert_not_called()


def test_missing_prompt(monkeypatch, isolated_llm, payload):
    def missing(*args):
        raise FileNotFoundError
    monkeypatch.setattr(agent, "load_prompt", missing)
    assert agent.design_agent(state(payload))["error"] == "Unable to load the design prompt."
    isolated_llm.responses.parse.assert_not_called()


def test_incomplete(isolated_llm, payload, design_payload):
    isolated_llm.responses.parse.return_value = SimpleNamespace(status="incomplete", output_parsed=design_payload)
    assert agent.design_agent(state(payload))["status"] == "failed"


def test_clear_through_design(tmp_path, monkeypatch, capsys, isolated_llm, payload, design_payload):
    monkeypatch.setattr(run, "RUNS_DIR", tmp_path / "runs")
    isolated_llm.responses.parse.side_effect = [response(payload), response(design_payload)]
    assert run.main(["start", "--through", "design", "--requirement", "Shorten URLs"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["status"] == "design_complete"
    directory = run.RUNS_DIR / report["run_id"]
    assert json.loads((directory / "design.json").read_text()) == design_payload
    assert json.loads((directory / "requirements.json").read_text()) == payload
    assert isolated_llm.responses.parse.call_count == 2


def test_paused_then_resumed_design(tmp_path, monkeypatch, capsys, isolated_llm, payload, design_payload):
    monkeypatch.setattr(run, "RUNS_DIR", tmp_path / "runs")
    ambiguous = copy.deepcopy(payload)
    ambiguous.update(user_stories=[], requires_human_clarification=True, open_questions=["What should users do?"])
    isolated_llm.responses.parse.side_effect = [response(ambiguous), response(payload), response(design_payload)]
    assert run.main(["start", "--through", "design", "--requirement", "Build a secure scalable app"]) == 0
    paused = json.loads(capsys.readouterr().out)
    assert paused["status"] == "paused"
    assert isolated_llm.responses.parse.call_count == 1
    assert not (run.RUNS_DIR / paused["run_id"] / "design.json").exists()
    assert run.main(["resume", "--run-id", paused["run_id"], "--answers", "Shorten URLs"]) == 0
    completed = json.loads(capsys.readouterr().out)
    assert completed["status"] == "design_complete"
    assert completed["thread_id"] == paused["thread_id"]
    assert completed["through"] == "design"
    assert len(completed["clarifications"]) == 1
    assert json.loads(isolated_llm.responses.parse.call_args.kwargs["input"][1]["content"])["clarifications"][0]["answers"] == "Shorten URLs"


@pytest.mark.parametrize("reject", [False, True])
def test_failure_or_rejection_skips_design(tmp_path, monkeypatch, capsys, isolated_llm, payload, reject):
    monkeypatch.setattr(run, "RUNS_DIR", tmp_path / "runs")
    if reject:
        payload.update(user_stories=[], requires_human_clarification=True, open_questions=["What behavior?"])
        isolated_llm.responses.parse.return_value = response(payload)
    else:
        isolated_llm.responses.parse.return_value = response(None)
    code = run.main(["start", "--through", "design", "--requirement", "Build an app"])
    report = json.loads(capsys.readouterr().out)
    if reject:
        assert run.main(["resume", "--run-id", report["run_id"], "--reject"]) == 0
        report = json.loads(capsys.readouterr().out)
        assert report["status"] == "rejected"
    else:
        assert code == 1
        assert report["status"] == "failed"
    assert isolated_llm.responses.parse.call_count == 1
    assert not (run.RUNS_DIR / report["run_id"] / "design.json").exists()


def test_design_failure_preserves_requirements(tmp_path, monkeypatch, capsys, isolated_llm, payload):
    monkeypatch.setattr(run, "RUNS_DIR", tmp_path / "runs")
    isolated_llm.responses.parse.side_effect = [response(payload), response({})]
    assert run.main(["start", "--through", "design", "--requirement", "Shorten URLs"]) == 1
    failed = json.loads(capsys.readouterr().out)
    assert failed["status"] == "failed"
    assert failed["requirements"] == payload
    assert failed["design"] is None
    assert json.loads((run.RUNS_DIR / failed["run_id"] / "requirements.json").read_text()) == payload
    assert not (run.RUNS_DIR / failed["run_id"] / "design.json").exists()


def test_requirements_only_never_calls_design(tmp_path, monkeypatch, capsys, isolated_llm, payload):
    monkeypatch.setattr(run, "RUNS_DIR", tmp_path / "runs")
    isolated_llm.responses.parse.return_value = response(payload)
    assert run.main(["start", "--requirement", "Shorten URLs"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["status"] == "requirements_complete"
    assert report["through"] == "requirements"
    assert report["design"] is None
    assert isolated_llm.responses.parse.call_count == 1
