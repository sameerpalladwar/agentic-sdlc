import copy
import json
from types import SimpleNamespace

import httpx
import pytest
from openai import AuthenticationError, BadRequestError
from langgraph.checkpoint.sqlite import SqliteSaver

import agents.design as agent
from models.requirements import RequirementsOutput
from orchestrator.design_validation import validate_design
from orchestrator.graph import build_graph
import run


def response(value):
    return SimpleNamespace(status="completed", output_parsed=value)


def invalid_missing_story(payload, design_payload):
    requirement = copy.deepcopy(payload)
    story = copy.deepcopy(payload["user_stories"][0])
    story.update(id="US-002", title="Redirect short URL")
    requirement["user_stories"].append(story)
    return requirement, copy.deepcopy(design_payload)


@pytest.mark.parametrize("corrections", [0, 1, 2])
def test_recovery_success(corrections, isolated_llm, payload, design_payload):
    requirements, bad = invalid_missing_story(payload, design_payload)
    good = copy.deepcopy(bad)
    good["story_traceability"].append({**copy.deepcopy(good["story_traceability"][0]), "story_id": "US-002"})
    isolated_llm.responses.parse.side_effect = [response(requirements), *[response(bad) for _ in range(corrections)], response(good)]
    result = build_graph().invoke({"requirement": "Shorten URLs", "status": "pending", "through": "design"})
    assert result["status"] == "design_complete"
    assert result["design_validation_status"] == "valid"
    assert result["design_retry_count"] == corrections
    assert len(result["recovery_history"]) == corrections + 1
    if corrections:
        first = result["recovery_history"][0]
        assert first["issues"][0]["code"] == "MISSING_STORY"
        assert first["issues"][0]["affected_story_ids"] == ["US-002"]
        message = json.loads(isolated_llm.responses.parse.call_args.kwargs["input"][1]["content"])
        assert message["requirements"] == requirements
        assert message["correction"]["current_invalid_design"] == bad
        assert message["correction"]["validation_issues"] == result["recovery_history"][-2]["issues"]


def test_retry_exhaustion(isolated_llm, payload, design_payload):
    requirements, bad = invalid_missing_story(payload, design_payload)
    isolated_llm.responses.parse.side_effect = [response(requirements), response(bad), response(bad), response(bad)]
    result = build_graph().invoke({"requirement": "Shorten URLs", "status": "pending", "through": "planning"})
    assert result["status"] == "failed"
    assert result["design"] is None
    assert result["design_retry_count"] == 2
    assert len(result["recovery_history"]) == 3
    assert "budget exhausted" in result["failure_reason"]
    assert isolated_llm.responses.parse.call_count == 4
    assert not result.get("plan")


def test_missing_story_precise(payload, design_payload):
    requirements, bad = invalid_missing_story(payload, design_payload)
    _, issues = validate_design(bad, RequirementsOutput.model_validate(requirements))
    assert issues == [{"code": "MISSING_STORY", "field": "story_traceability", "message": "Add exactly one traceability entry for US-002.", "retryable": True, "affected_story_ids": ["US-002"]}]


@pytest.mark.parametrize("problem, code", [("unknown", "UNKNOWN_STORY"), ("duplicate", "DUPLICATE_STORY"),
    ("component", "INVALID_DESIGN_REFERENCE"), ("dependency", "INVALID_COMPONENT_REFERENCE"),
    ("coverage", "CRITERION_COVERAGE"), ("schema", "SCHEMA_INVALID"), ("empty", "EMPTY_CRITERION_COVERAGE")])
def test_structured_issues(problem, code, payload, design_payload):
    if problem == "unknown":
        design_payload["story_traceability"][0]["story_id"] = "INVENTED"
    elif problem == "duplicate":
        design_payload["story_traceability"] *= 2
    elif problem == "component":
        design_payload["story_traceability"][0]["design_elements"] = ["component:Invented"]
    elif problem == "dependency":
        design_payload["components"][0]["dependencies"] = ["Unknown"]
    elif problem == "coverage":
        design_payload["story_traceability"][0]["acceptance_criteria_coverage"] *= 2
    elif problem == "empty":
        design_payload["story_traceability"][0]["acceptance_criteria_coverage"] = [" "]
    else:
        del design_payload["architecture_summary"]
    _, issues = validate_design(design_payload, RequirementsOutput.model_validate(payload))
    assert code in {i["code"] for i in issues}
    assert all(set(i) == {"code", "field", "message", "retryable", "affected_story_ids"} for i in issues)


def test_explicit_architecture_constraints(payload, design_payload):
    _, issues = validate_design(design_payload, RequirementsOutput.model_validate(payload), "Use Django. POST /shorten and GET /{short_code}.")
    codes = [i["code"] for i in issues]
    assert codes.count("REQUIRED_ENDPOINT") == 2
    assert "REQUIRED_TECHNOLOGY" in codes


@pytest.mark.parametrize("kind", ["permission", "contract"])
def test_nonretryable_api_error(kind, isolated_llm, payload):
    response_obj = httpx.Response(401 if kind == "permission" else 400, request=httpx.Request("POST", "https://api.openai.com"))
    exception = AuthenticationError if kind == "permission" else BadRequestError
    isolated_llm.responses.parse.side_effect = [response(payload), exception("secret server details", response=response_obj, body=None)]
    result = build_graph().invoke({"requirement": "Shorten URLs", "status": "pending", "through": "design"})
    assert result["status"] == "failed"
    assert result["design_retry_count"] == 0
    assert not result["design_validation_issues"][0]["retryable"]
    assert "secret server details" not in json.dumps(result)
    assert isolated_llm.responses.parse.call_count == 2


def test_nonretryable_configuration(monkeypatch, isolated_llm, payload):
    monkeypatch.delenv("OPENAI_MODEL")
    # Enter with existing approved requirements, so missing config is detected by design itself.
    generated = agent.design_agent({"requirement": "Shorten URLs", "requirements": payload, "status": "requirements_complete"})
    assert generated["design_generation_issues"][0]["code"] == "CONFIGURATION"
    assert generated["design_generation_issues"][0]["retryable"] is False
    isolated_llm.responses.parse.assert_not_called()


def test_contract_programming_error(monkeypatch, isolated_llm, payload):
    def broken(*args, **kwargs):
        raise TypeError("programming detail")
    monkeypatch.setattr(agent.DesignOutput, "model_json_schema", broken)
    result = agent.design_agent({"requirement": "Shorten URLs", "requirements": payload, "status": "requirements_complete"})
    assert result["design_generation_issues"][0]["code"] == "SCHEMA_CONTRACT"
    isolated_llm.responses.parse.assert_not_called()


def test_security_error_is_redacted(isolated_llm, payload, design_payload):
    design_payload["assumptions"] = ["sk-shouldneverappear1234567890"]
    isolated_llm.responses.parse.side_effect = [response(payload), response(design_payload)]
    result = build_graph().invoke({"requirement": "Shorten URLs", "status": "pending", "through": "design"})
    assert result["design_validation_issues"][0]["code"] == "SECURITY_SECRET"
    assert result["design_retry_count"] == 0
    assert "sk-shouldneverappear" not in json.dumps(result)


def test_recovery_artifacts_and_checkpoint(tmp_path, monkeypatch, capsys, isolated_llm, payload, design_payload):
    monkeypatch.setattr(run, "RUNS_DIR", tmp_path / "runs")
    requirements, bad = invalid_missing_story(payload, design_payload)
    good = copy.deepcopy(bad)
    good["story_traceability"].append({**copy.deepcopy(good["story_traceability"][0]), "story_id": "US-002"})
    isolated_llm.responses.parse.side_effect = [response(requirements), response(bad), response(good)]
    assert run.main(["start", "--through", "design", "--requirement", "Shorten URLs"]) == 0
    report = json.loads(capsys.readouterr().out)
    directory = run.RUNS_DIR / report["run_id"]
    assert report["design_retry_count"] == 1
    assert (directory / "design_attempts/attempt_001.json").is_file()
    assert (directory / "design_attempts/attempt_002.json").is_file()
    assert json.loads((directory / "recovery_history.json").read_text()) == report["recovery_history"]
    with SqliteSaver.from_conn_string(str(directory / "checkpoints.sqlite")) as saver:
        snapshot = build_graph(saver).get_state({"configurable": {"thread_id": report["thread_id"]}})
        assert snapshot.values["design_retry_count"] == 1
        assert snapshot.values["recovery_history"] == report["recovery_history"]


def test_continues_to_planning(isolated_llm, payload, design_payload):
    bad = copy.deepcopy(design_payload)
    bad["story_traceability"][0]["story_id"] = "UNKNOWN"
    task = {"task_id": "T1", "title": "Implement", "description": "Implement URL shortening", "related_user_story_ids": ["US-001"],
            "dependencies": [], "expected_artifacts": ["workspace/output_code/main.py"], "acceptance_checks": ["Creates short URLs"], "status": "pending"}
    plan = {"tasks": [task], "shared_artifacts": []}
    isolated_llm.responses.parse.side_effect = [response(payload), response(bad), response(design_payload), response(plan)]
    result = build_graph().invoke({"requirement": "Shorten URLs", "status": "pending", "through": "planning"})
    assert result["status"] == "planning_complete"
    assert result["design_retry_count"] == 1
    assert result["plan"]["tasks"][0]["status"] == "ready"
