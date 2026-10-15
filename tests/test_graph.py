from types import SimpleNamespace

import httpx
import pytest
from openai import APIConnectionError, APIStatusError

import agents.requirements as agent
from models.requirements import RequirementsOutput
from orchestrator.graph import build_graph


def invoke(requirement="Build a URL shortener service"):
    return build_graph().invoke({"requirement": requirement, "status": "pending"})


def test_valid_requirement(llm_response, payload):
    result = invoke()
    assert result["status"] == "requirements_complete"
    assert result["requirements"] == payload
    assert result["error"] is None
    kwargs = llm_response.call_args.kwargs
    assert kwargs["model"] == "test-model"
    assert kwargs["text_format"] is RequirementsOutput
    assert kwargs["input"][0]["content"] == agent.load_prompt()
    assert kwargs["input"][1] == {"role": "user", "content": "Build a URL shortener service"}


def test_clarification(llm_response, payload):
    payload.update(user_stories=[], requires_human_clarification=True,
                   open_questions=["Who are the users?"])
    result = invoke("Build something useful")
    assert result["status"] == "paused"
    assert result["__interrupt__"]
    assert result["requirements"]["open_questions"] == ["Who are the users?"]


@pytest.mark.parametrize("requirement", ["", "  \n\t"])
def test_empty_input(requirement, isolated_llm):
    result = invoke(requirement)
    assert result["status"] == "failed"
    assert result["requirements"] is None
    isolated_llm.responses.parse.assert_not_called()


@pytest.mark.parametrize("name", ["OPENAI_API_KEY", "OPENAI_MODEL"])
def test_missing_configuration(name, monkeypatch, isolated_llm):
    monkeypatch.delenv(name)
    assert invoke()["status"] == "failed"
    isolated_llm.responses.parse.assert_not_called()


@pytest.mark.parametrize("parsed", [None, {}, "not JSON", {"summary": "incomplete"}])
def test_invalid_output(parsed, isolated_llm):
    isolated_llm.responses.parse.return_value = SimpleNamespace(status="completed", output_parsed=parsed)
    result = invoke()
    assert result["status"] == "failed"
    assert result["requirements"] is None


def test_incomplete_response(llm_response, isolated_llm):
    isolated_llm.responses.parse.return_value.status = "incomplete"
    assert invoke()["status"] == "failed"


@pytest.mark.parametrize("error", [
    APIConnectionError(request=httpx.Request("POST", "https://api.openai.com")),
    APIStatusError("secret server detail", response=httpx.Response(429, request=httpx.Request("POST", "https://api.openai.com")), body=None),
    ValueError("secret invalid JSON"),
])
def test_api_and_parse_errors(error, isolated_llm):
    isolated_llm.responses.parse.side_effect = error
    result = invoke()
    assert result["status"] == "failed"
    assert "secret" not in result["error"]
    assert result["requirements"] is None


def test_missing_prompt(monkeypatch, isolated_llm):
    def missing():
        raise FileNotFoundError("private path")
    monkeypatch.setattr(agent, "load_prompt", missing)
    assert invoke()["error"] == "Unable to load the requirements prompt."
    isolated_llm.responses.parse.assert_not_called()


def test_failure_clears_stale_requirements():
    result = build_graph().invoke({"requirement": "", "status": "requirements_complete",
                                   "requirements": {"stale": True}})
    assert result["requirements"] is None
    assert result["status"] == "failed"


def test_graph_topology():
    graph = build_graph().get_graph()
    assert {(edge.source, edge.target) for edge in graph.edges} == {
        ("__start__", "requirements"), ("requirements", "__end__"),
        ("requirements", "design"), ("design", "__end__"),
        ("design", "planning"), ("planning", "__end__"),
        ("requirements", "pause"), ("pause", "clarification"),
        ("clarification", "requirements"), ("clarification", "__end__"),
    }
