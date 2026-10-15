import copy
import json
from types import SimpleNamespace

import httpx
import pytest
from openai import APIConnectionError
from pydantic import ValidationError

import agents.planning as agent
from models.planning import ExecutionPlan, ImplementationTask
from models.requirements import RequirementsOutput
from orchestrator.task_plan import calculate_readiness, validate_execution_plan
import run


@pytest.fixture
def plan_payload():
    def task(task_id, dependencies, path):
        return {"task_id": task_id, "title": task_id, "description": "Implement and verify the URL service",
                "related_user_story_ids": ["US-001"], "dependencies": dependencies,
                "expected_artifacts": ["workspace/output_code/" + path],
                "acceptance_checks": ["Valid URL returns a usable short URL"], "status": "pending"}
    return {"tasks": [task("T3", ["T2"], "tests/test_api.py"),
                      task("T1", [], "app/storage.py"), task("T2", ["T1"], "app/main.py")],
            "shared_artifacts": [], "ordered_task_ids": [], "ready_task_ids": [], "blocked_task_ids": []}


def response(value):
    return SimpleNamespace(status="completed", output_parsed=value)


def input_state(payload, design_payload):
    return {"requirement": "Shorten URLs", "requirements": payload, "design": design_payload,
            "status": "design_complete", "through": "planning"}


def validated(plan_payload, payload):
    return validate_execution_plan(ExecutionPlan.model_validate(plan_payload), RequirementsOutput.model_validate(payload))


def test_valid_plan(plan_payload, payload):
    plan = validated(plan_payload, payload)
    assert plan.ordered_task_ids == ["T1", "T2", "T3"]
    assert [t.task_id for t in plan.tasks] == plan.ordered_task_ids
    assert plan.ready_task_ids == ["T1"]
    assert plan.blocked_task_ids == ["T2", "T3"]
    assert [t.status for t in plan.tasks] == ["ready", "blocked", "blocked"]
    assert ExecutionPlan.model_validate_json(plan.model_dump_json()) == plan


@pytest.mark.parametrize("problem", ["missing_dependency", "cycle", "self", "duplicate_id", "repeated_dependency",
                                     "shared_file", "missing_story", "unknown_story"])
def test_invalid_plan(problem, plan_payload, payload):
    tasks = plan_payload["tasks"]
    if problem == "missing_dependency":
        tasks[0]["dependencies"] = ["MISSING"]
    elif problem == "cycle":
        tasks[1]["dependencies"] = ["T3"]
    elif problem == "self":
        tasks[0]["dependencies"] = ["T3"]
    elif problem == "duplicate_id":
        tasks[0]["task_id"] = "T1"
    elif problem == "repeated_dependency":
        tasks[0]["dependencies"] *= 2
    elif problem == "shared_file":
        tasks[0]["expected_artifacts"] = tasks[1]["expected_artifacts"]
    elif problem == "missing_story":
        for task in tasks:
            task["related_user_story_ids"] = []
    else:
        tasks[0]["related_user_story_ids"] = ["US-999"]
    with pytest.raises(ValueError):
        validated(plan_payload, payload)


def test_justified_shared_artifact(plan_payload, payload):
    tasks = plan_payload["tasks"]
    path = tasks[1]["expected_artifacts"][0]
    tasks[2]["expected_artifacts"] = [path]
    plan_payload["shared_artifacts"] = [{"path": path, "task_ids": ["T1", "T2"],
                                         "justification": "T1 creates storage; T2 integrates the API after T1."}]
    assert validated(plan_payload, payload).ordered_task_ids == ["T1", "T2", "T3"]


@pytest.mark.parametrize("problem", ["wrong_owners", "blank", "unused", "duplicate"])
def test_invalid_justification(problem, plan_payload, payload):
    path = plan_payload["tasks"][1]["expected_artifacts"][0]
    plan_payload["tasks"][2]["expected_artifacts"] = [path]
    item = {"path": path, "task_ids": ["T1", "T2"], "justification": "Ordered edits"}
    plan_payload["shared_artifacts"] = [item]
    if problem == "wrong_owners":
        item["task_ids"] = ["T1", "T3"]
    elif problem == "blank":
        item["justification"] = " "
    elif problem == "unused":
        item["path"] = "workspace/output_code/unused.py"
    else:
        plan_payload["shared_artifacts"].append(copy.deepcopy(item))
    with pytest.raises(ValueError):
        validated(plan_payload, payload)


@pytest.mark.parametrize("path", ["/tmp/file.py", "workspace/output_code/../secret", "workspace/output_code",
                                  "workspace/output_code/a/./b.py", "workspace/output_code//a.py",
                                  "workspace/output_code/a\\b.py", "README.md"])
def test_invalid_artifact_path(path, plan_payload):
    plan_payload["tasks"][0]["expected_artifacts"] = [path]
    with pytest.raises(ValidationError):
        ExecutionPlan.model_validate(plan_payload)


@pytest.mark.parametrize("checks", [[], [" "]])
def test_acceptance_checks_required(checks, plan_payload):
    plan_payload["tasks"][0]["acceptance_checks"] = checks
    with pytest.raises(ValidationError):
        ExecutionPlan.model_validate(plan_payload)


def test_llm_cannot_claim_execution(plan_payload, payload):
    for task in plan_payload["tasks"]:
        task["status"] = "completed"
    plan_payload["ordered_task_ids"] = ["fake"]
    result = validated(plan_payload, payload)
    assert result.ready_task_ids == ["T1"]
    assert result.ordered_task_ids == ["T1", "T2", "T3"]
    result.tasks[0].status = "completed"
    assert calculate_readiness(result.tasks) == (["T2"], ["T3"])


def test_wont_story_not_required(plan_payload, payload):
    excluded = copy.deepcopy(payload["user_stories"][0])
    excluded.update(id="US-002", priority="wont")
    payload["user_stories"].append(excluded)
    assert validated(plan_payload, payload)
    plan_payload["tasks"][0]["related_user_story_ids"] = ["US-002"]
    with pytest.raises(ValueError, match="excluded"):
        validated(plan_payload, payload)


def test_successful_agent(isolated_llm, payload, design_payload, plan_payload):
    isolated_llm.responses.parse.return_value = response(plan_payload)
    result = agent.planning_agent(input_state(payload, design_payload))
    assert result["status"] == "planning_complete"
    assert result["plan"]["ordered_task_ids"] == ["T1", "T2", "T3"]
    kwargs = isolated_llm.responses.parse.call_args.kwargs
    assert kwargs["text_format"] is ExecutionPlan
    assert json.loads(kwargs["input"][1]["content"]) == {"requirements": payload, "design": design_payload}
    assert kwargs["input"][0]["content"] == agent.load_prompt(agent.PLANNING_PROMPT)
    assert isolated_llm.responses.parse.call_count == 1


@pytest.mark.parametrize("problem", ["missing_design", "invalid_design", "missing_requirements", "failed", "paused", "ambiguous"])
def test_ineligible_input(problem, isolated_llm, payload, design_payload):
    state = input_state(payload, design_payload)
    if problem == "missing_design":
        state.pop("design")
    elif problem == "invalid_design":
        state["design"] = {}
    elif problem == "missing_requirements":
        state.pop("requirements")
    elif problem == "ambiguous":
        payload.update(requires_human_clarification=True, open_questions=["What behavior?"])
    else:
        state["status"] = problem
    assert agent.planning_agent(state)["status"] == "failed"
    isolated_llm.responses.parse.assert_not_called()


@pytest.mark.parametrize("value", [None, {}, "invalid"])
def test_invalid_response(value, isolated_llm, payload, design_payload):
    isolated_llm.responses.parse.return_value = response(value)
    result = agent.planning_agent(input_state(payload, design_payload))
    assert result["status"] == "failed"
    assert result["plan"] is None


def test_llm_error(isolated_llm, payload, design_payload):
    isolated_llm.responses.parse.side_effect = APIConnectionError(request=httpx.Request("POST", "https://api.openai.com"))
    result = agent.planning_agent(input_state(payload, design_payload))
    assert result["status"] == "failed"
    assert "OpenAI planning request failed" in result["error"]


def test_cycle_rejected_by_agent(isolated_llm, payload, design_payload, plan_payload):
    plan_payload["tasks"][1]["dependencies"] = ["T3"]
    isolated_llm.responses.parse.return_value = response(plan_payload)
    result = agent.planning_agent(input_state(payload, design_payload))
    assert result["status"] == "failed"
    assert result["plan"] is None
    assert "cycle" in result["error"]


def test_start_through_planning(tmp_path, monkeypatch, capsys, isolated_llm, payload, design_payload, plan_payload):
    monkeypatch.setattr(run, "RUNS_DIR", tmp_path / "runs")
    isolated_llm.responses.parse.side_effect = [response(payload), response(design_payload), response(plan_payload)]
    assert run.main(["start", "--stop-after", "planning", "--requirement", "Shorten URLs"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "planning_complete"
    directory = run.RUNS_DIR / result["run_id"]
    assert json.loads((directory / "plan.json").read_text()) == result["plan"]
    assert json.loads((directory / "design.json").read_text()) == design_payload
    assert isolated_llm.responses.parse.call_count == 3


def test_resume_through_planning(tmp_path, monkeypatch, capsys, isolated_llm, payload, design_payload, plan_payload):
    monkeypatch.setattr(run, "RUNS_DIR", tmp_path / "runs")
    ambiguous = copy.deepcopy(payload)
    ambiguous.update(user_stories=[], requires_human_clarification=True, open_questions=["What behavior?"])
    isolated_llm.responses.parse.side_effect = [response(ambiguous), response(payload), response(design_payload), response(plan_payload)]
    assert run.main(["start", "--through", "planning", "--requirement", "Build an app"]) == 0
    paused = json.loads(capsys.readouterr().out)
    assert paused["status"] == "paused"
    assert isolated_llm.responses.parse.call_count == 1
    assert not (run.RUNS_DIR / paused["run_id"] / "plan.json").exists()
    assert run.main(["resume", "--run-id", paused["run_id"], "--answers", "Shorten URLs"]) == 0
    resumed = json.loads(capsys.readouterr().out)
    assert resumed["status"] == "planning_complete"
    assert resumed["thread_id"] == paused["thread_id"]
    assert resumed["through"] == "planning"


def test_design_failure_skips_planning(tmp_path, monkeypatch, capsys, isolated_llm, payload):
    monkeypatch.setattr(run, "RUNS_DIR", tmp_path / "runs")
    isolated_llm.responses.parse.side_effect = [response(payload), response(None), response(None), response(None)]
    assert run.main(["--requirement", "Shorten URLs", "--through", "planning"]) == 1
    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "failed"
    assert result["plan"] is None
    assert isolated_llm.responses.parse.call_count == 4


@pytest.mark.parametrize("name", ["OPENAI_API_KEY", "OPENAI_MODEL"])
def test_missing_configuration(name, monkeypatch, isolated_llm, payload, design_payload):
    monkeypatch.delenv(name)
    assert agent.planning_agent(input_state(payload, design_payload))["status"] == "failed"
    isolated_llm.responses.parse.assert_not_called()


def test_missing_prompt(monkeypatch, isolated_llm, payload, design_payload):
    def missing(*args):
        raise FileNotFoundError
    monkeypatch.setattr(agent, "load_prompt", missing)
    assert agent.planning_agent(input_state(payload, design_payload))["error"] == "Unable to load the planning prompt."
    isolated_llm.responses.parse.assert_not_called()


def test_parse_error_redacted(isolated_llm, payload, design_payload):
    isolated_llm.responses.parse.side_effect = ValueError("secret response contents")
    result = agent.planning_agent(input_state(payload, design_payload))
    assert result["status"] == "failed"
    assert "secret" not in result["error"]


def test_incomplete_response(isolated_llm, payload, design_payload, plan_payload):
    isolated_llm.responses.parse.return_value = SimpleNamespace(status="incomplete", output_parsed=plan_payload)
    assert agent.planning_agent(input_state(payload, design_payload))["status"] == "failed"


def test_multiple_ready_roots(plan_payload, payload):
    plan_payload["tasks"][2]["dependencies"] = []
    plan = validated(plan_payload, payload)
    assert set(plan.ready_task_ids) == {"T1", "T2"}
    assert plan.blocked_task_ids == ["T3"]


def test_cyclic_cli_failure_preserves_inputs(tmp_path, monkeypatch, capsys, isolated_llm, payload, design_payload, plan_payload):
    monkeypatch.setattr(run, "RUNS_DIR", tmp_path / "runs")
    plan_payload["tasks"][1]["dependencies"] = ["T3"]
    isolated_llm.responses.parse.side_effect = [response(payload), response(design_payload), response(plan_payload)]
    assert run.main(["start", "--through", "planning", "--requirement", "Shorten URLs"]) == 1
    report = json.loads(capsys.readouterr().out)
    assert report["status"] == "failed"
    assert "cycle" in report["error"]
    directory = run.RUNS_DIR / report["run_id"]
    assert json.loads((directory / "design.json").read_text()) == design_payload
    assert json.loads((directory / "requirements.json").read_text()) == payload
    assert not (directory / "plan.json").exists()


def test_rejection_skips_planning(tmp_path, monkeypatch, capsys, isolated_llm, payload):
    monkeypatch.setattr(run, "RUNS_DIR", tmp_path / "runs")
    payload.update(user_stories=[], requires_human_clarification=True, open_questions=["What behavior?"])
    isolated_llm.responses.parse.return_value = response(payload)
    assert run.main(["start", "--through", "planning", "--requirement", "Build an app"]) == 0
    paused = json.loads(capsys.readouterr().out)
    assert run.main(["resume", "--run-id", paused["run_id"], "--reject"]) == 0
    rejected = json.loads(capsys.readouterr().out)
    assert rejected["status"] == "rejected"
    assert isolated_llm.responses.parse.call_count == 1
    assert not (run.RUNS_DIR / paused["run_id"] / "plan.json").exists()
