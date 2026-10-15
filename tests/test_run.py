import json
from types import SimpleNamespace

import pytest

import run


def call(capsys, arguments):
    code = run.main(arguments)
    captured = capsys.readouterr()
    return code, json.loads(captured.out or captured.err)


@pytest.fixture(autouse=True)
def runs(tmp_path, monkeypatch):
    monkeypatch.setattr(run, "RUNS_DIR", tmp_path / "runs")


@pytest.mark.parametrize("clarification", [False, True])
def test_start_saves_json(capsys, llm_response, payload, clarification):
    if clarification:
        payload.update(requires_human_clarification=True, open_questions=["Which users?"])
    code, report = call(capsys, ["start", "--requirement", "Shorten URLs"])
    assert code == 0
    assert report["status"] == ("paused" if clarification else "requirements_complete")
    assert report["thread_id"] == report["run_id"]
    directory = run.RUNS_DIR / report["run_id"]
    assert json.loads((directory / "requirements.json").read_text()) == payload
    assert json.loads((directory / "status.json").read_text()) == report
    assert (directory / "checkpoints.sqlite").is_file()


def test_resume_after_reopening_checkpoint(capsys, llm_response, payload):
    payload.update(requires_human_clarification=True, open_questions=["Which users?"])
    _, paused = call(capsys, ["start", "--requirement", "Build an app"])
    payload.update(requires_human_clarification=False, open_questions=[])
    code, done = call(capsys, ["resume", "--run-id", paused["run_id"], "--answers", "Public users; shorten URLs."])
    assert code == 0
    assert done["status"] == "requirements_complete"
    assert done["run_id"] == paused["run_id"]
    assert done["clarifications"] == [{"questions": ["Which users?"], "answers": "Public users; shorten URLs."}]
    assert llm_response.call_count == 2
    messages = llm_response.call_args.kwargs["input"]
    assert messages[1]["content"] == "Build an app"
    assert "Public users; shorten URLs." in messages[2]["content"]
    assert json.loads((run.RUNS_DIR / done["run_id"] / "requirements.json").read_text()) == payload
    code, report = call(capsys, ["resume", "--run-id", done["run_id"], "--answers", "again"])
    assert code == 1
    assert "not paused" in report["error"]
    assert llm_response.call_count == 2


def test_rejection(capsys, llm_response, payload):
    payload.update(requires_human_clarification=True, open_questions=["Which users?"])
    _, paused = call(capsys, ["start", "--requirement", "Build an app"])
    code, rejected = call(capsys, ["resume", "--run-id", paused["run_id"], "--reject"])
    assert code == 0
    assert rejected["status"] == "rejected"
    assert llm_response.call_count == 1
    code, _ = call(capsys, ["resume", "--run-id", paused["run_id"], "--reject"])
    assert code == 1


def test_repeated_clarification(capsys, llm_response, payload):
    payload.update(requires_human_clarification=True, open_questions=["Which users?"])
    _, first = call(capsys, ["start", "--requirement", "Build an app"])
    payload["open_questions"] = ["What behavior?"]
    _, second = call(capsys, ["resume", "--run-id", first["run_id"], "--answers", "Public users"])
    assert second["status"] == "paused"
    assert second["open_questions"] == ["What behavior?"]
    payload.update(requires_human_clarification=False, open_questions=[])
    _, third = call(capsys, ["resume", "--run-id", first["run_id"], "--answers", "Shorten URLs"])
    assert third["status"] == "requirements_complete"
    assert len(third["clarifications"]) == 2


@pytest.mark.parametrize("run_id", ["../outside", "a" * 32])
def test_unknown_or_invalid_run(capsys, run_id, isolated_llm):
    code, _ = call(capsys, ["resume", "--run-id", run_id, "--reject"])
    assert code == 1
    isolated_llm.responses.parse.assert_not_called()


def test_blank_answer_keeps_pause(capsys, llm_response, payload):
    payload.update(requires_human_clarification=True, open_questions=["Which users?"])
    _, report = call(capsys, ["start", "--requirement", "Build an app"])
    code, _ = call(capsys, ["resume", "--run-id", report["run_id"], "--answers", " "])
    assert code == 1
    _, final = call(capsys, ["resume", "--run-id", report["run_id"], "--reject"])
    assert final["status"] == "rejected"


def test_llm_failure_after_resume(capsys, llm_response, payload):
    payload.update(requires_human_clarification=True, open_questions=["Which users?"])
    _, paused = call(capsys, ["start", "--requirement", "Build an app"])
    llm_response.return_value = SimpleNamespace(status="completed", output_parsed=None)
    code, failed = call(capsys, ["resume", "--run-id", paused["run_id"], "--answers", "Public users"])
    assert code == 1
    assert failed["status"] == "failed"
    assert failed["requirements"] is None


def test_empty_input(capsys, isolated_llm):
    code, report = call(capsys, ["start", "--requirement", " "])
    assert code == 1
    assert report["status"] == "failed"
    assert not run.RUNS_DIR.exists()
    isolated_llm.responses.parse.assert_not_called()


def test_cli_requires_argument():
    with pytest.raises(SystemExit) as error:
        run.main([])
    assert error.value.code == 2


def test_save_failure(tmp_path, monkeypatch, capsys, llm_response):
    blocked = tmp_path / "file"
    blocked.write_text("existing")
    monkeypatch.setattr(run, "RUNS_DIR", blocked)
    code, report = call(capsys, ["start", "--requirement", "Shorten URLs"])
    assert code == 1
    assert "Unable to save" in report["error"]


def test_legacy_cli(capsys, llm_response):
    code, report = call(capsys, ["--requirement", "Shorten URLs"])
    assert code == 0
    assert report["status"] == "requirements_complete"


def test_resume_across_processes(tmp_path):
    """Both CLI processes use real SQLite checkpoints and an offline fake LLM node."""
    import subprocess
    import sys
    script = '''
import sys
from pathlib import Path
import run
import orchestrator.graph as workflow

def offline_requirements(state):
    clarified = bool(state.get("clarifications"))
    ambiguous = state["requirement"] == "Build an app" and not clarified
    return {"status": "needs_human_clarification" if ambiguous else "requirements_complete",
            "requirements": {"summary": "Offline demonstration", "open_questions": ["What should users do?"] if ambiguous else [],
                             "requires_human_clarification": ambiguous}, "error": None}

workflow.requirements_agent = offline_requirements
run.RUNS_DIR = Path(sys.argv[1])
raise SystemExit(run.main(sys.argv[2:]))
'''
    def process(*args):
        completed = subprocess.run([sys.executable, "-c", script, str(tmp_path / "runs"), *args],
                                   capture_output=True, text=True, check=True)
        return json.loads(completed.stdout)
    clear = process("start", "--requirement", "Shorten URLs")
    assert clear["status"] == "requirements_complete"
    paused = process("start", "--requirement", "Build an app")
    assert paused["status"] == "paused"
    resumed = process("resume", "--run-id", paused["run_id"], "--answers", "Shorten URLs")
    assert resumed["status"] == "requirements_complete"
    assert resumed["thread_id"] == paused["thread_id"]
    rejected_start = process("start", "--requirement", "Build an app")
    rejected = process("resume", "--run-id", rejected_start["run_id"], "--reject")
    assert rejected["status"] == "rejected"
    print("\nOffline CLI demonstration (separate processes, SQLite):")
    print("Clear:", clear["status"])
    print("Ambiguous:", paused["status"], paused["open_questions"])
    print("Resume:", resumed["status"], "same thread:", resumed["thread_id"] == paused["thread_id"])
    print("Reject:", rejected["status"])
