"""The CLI defaults to delivery, preserving partial targets and mandatory human gates."""

import copy
import json
import shlex
from types import SimpleNamespace

import pytest

import run
from models.qa import QAResult
from orchestrator import file_writer
from tests.conftest import approved_review
from tests.test_qa import completed_state


@pytest.fixture
def workflow_responses(tmp_path, monkeypatch, isolated_llm, payload, design_payload):
    monkeypatch.setattr(run, "RUNS_DIR", tmp_path / "runs")
    monkeypatch.setattr(file_writer, "OUTPUT_ROOT", tmp_path / "output")
    fixture = completed_state(payload, design_payload, "a" * 32)
    fixture["plan"]["tasks"][0]["status"] = "pending"
    monkeypatch.setattr("agents.reviewer.generate_review", approved_review)
    return [payload, design_payload, fixture["plan"], fixture["development_results"][0]["result"]]


def responses(client, values):
    client.responses.parse.side_effect = [
        SimpleNamespace(status="completed", output_parsed=value) for value in values
    ]


@pytest.mark.parametrize("legacy", [False, True])
@pytest.mark.parametrize("clarify", [False, True])
def test_default_workflow_and_explicit_human_approval(
    workflow_responses, isolated_llm, monkeypatch, capsys, legacy, clarify
):
    values = workflow_responses
    if clarify:
        ambiguous = copy.deepcopy(values[0])
        ambiguous.update(requires_human_clarification=True, open_questions=["Which users?"])
        values = [ambiguous, *values]
    responses(isolated_llm, values)
    prefix = [] if legacy else ["start"]
    code = run.main([*prefix, "--requirement", "Build a URL shortener", "--json"])
    result = json.loads(capsys.readouterr().out)
    run_id = result["run_id"]
    assert result["through"] == "delivery"
    if clarify:
        assert code == 0 and result["status"] == "paused"
        assert result["open_questions"] == ["Which users?"]
        assert run_id in result["pause"]["resume_command"]
        assert not result["design"]
        code = run.main(["resume", "--run-id", run_id, "--answers", "Public users", "--json"])
        result = json.loads(capsys.readouterr().out)
        assert result["run_id"] == run_id and result["through"] == "delivery"
    assert code == 3 and result["status"] == "validation_blocked"
    assert result["qa_result"]["status"] == "qa_blocked"
    assert result["review_result"]["status"] == "approved"
    assert not result["human_approval"] and not result["final_manifest"]
    events = [
        json.loads(line)
        for line in (run.RUNS_DIR / run_id / "events.jsonl").read_text().splitlines()
    ]
    stages = [e["stage"] for e in events if e["event"] == "agent_started"]
    for stage in (
        "project_routing",
        "requirements",
        "design",
        "planning",
        "developer",
        "qa",
        "review",
        "validation_gate",
    ):
        assert stage in stages
    assert stages.index("developer") < min(stages.index("qa"), stages.index("review"))
    assert stages.index("validation_gate") > max(stages.index("qa"), stages.index("review"))
    monkeypatch.setattr(
        "agents.qa.run_tests",
        lambda *a, **k: QAResult(status="qa_passed", exit_code=0, total_tests=1, passed_tests=1),
    )
    assert run.main(["qa", "--run-id", run_id, "--authorize-local-execution", "--json"]) == 0
    pending = json.loads(capsys.readouterr().out)
    assert pending["status"] == "paused" and pending["pause"]["kind"] == "approval"
    assert not pending["human_approval"] and not pending["final_manifest"]
    command = shlex.split(pending["pause"]["resume_command"])[2:]
    assert run.parser().parse_args(command).revision == pending["validation_revision"]
    assert run.main([*command, "--json"]) == 0
    delivered = json.loads(capsys.readouterr().out)
    assert delivered["status"] == "completed" and delivered["run_id"] == run_id
    assert delivered["human_approval"]["decision"] == "approve"
    assert (run.RUNS_DIR / run_id / "final_manifest.json").is_file()


@pytest.mark.parametrize("option", ["--through", "--stop-after"])
@pytest.mark.parametrize(
    "stage,count", [("requirements", 1), ("design", 2), ("planning", 3), ("development", 4)]
)
def test_explicit_partial_target(workflow_responses, isolated_llm, capsys, option, stage, count):
    responses(isolated_llm, workflow_responses[:count])
    assert (
        run.main(["start", "--requirement", "Build a URL shortener", option, stage, "--json"]) == 0
    )
    result = json.loads(capsys.readouterr().out)
    assert result["status"] == f"{stage}_complete" and result["through"] == stage
    assert not result["qa_result"] and not result["human_approval"]
    assert isolated_llm.responses.parse.call_count == count


def test_failure_names_stage_and_logs(workflow_responses, isolated_llm, capsys):
    responses(isolated_llm, [{}])
    assert run.main(["start", "--requirement", "Build a URL shortener"]) == 1
    captured = capsys.readouterr()
    assert "Failed stage: requirements" in captured.out
    assert "execution.log" in captured.out and "events.jsonl" in captured.out
    assert "[running] Requirements:" in captured.err and "[failed] Requirements:" in captured.err
    assert "Run " in captured.err


def test_clarification_terminal_command(workflow_responses, isolated_llm, capsys):
    ambiguous = copy.deepcopy(workflow_responses[0])
    ambiguous.update(requires_human_clarification=True, open_questions=["Which users?"])
    responses(isolated_llm, [ambiguous])
    assert run.main(["start", "--requirement", "Build a URL shortener"]) == 0
    output = capsys.readouterr().out
    run_id = next(run.RUNS_DIR.iterdir()).name
    assert "Paused:" in output and "Question: Which users?" in output
    assert f"python run.py resume --run-id {run_id} --answers" in output
