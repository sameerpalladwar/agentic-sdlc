"""Progress is a redacted projection of actual node results, never an extra model call."""

import io
import json

import pytest

from orchestrator.observability import RunObserver, observe_node
from orchestrator.progress import ProgressRenderer
from orchestrator.summaries import error_details, stage_summary

RUN = "e" * 32
REVISION = "f" * 64


def observed(tmp_path, stage, before, output):
    stream = io.StringIO()
    observer = RunObserver(RUN, tmp_path, ProgressRenderer(stream, ansi=False))
    result = observe_node(stage, lambda state: output)({"run_id": RUN, **before})
    observer.close()
    events = [json.loads(line) for line in (tmp_path / "events.jsonl").read_text().splitlines()]
    return result, stream.getvalue(), events


def test_requirements_summary_counts_actual_values(tmp_path):
    output = {
        "status": "requirements_complete",
        "requirements": {
            "user_stories": [{"acceptance_criteria": [{}, {}]}, {"acceptance_criteria": [{}]}],
            "open_questions": ["Question one"],
        },
    }
    result, text, events = observed(tmp_path, "requirements", {}, output)
    assert result is output
    assert "[started] Requirements:" in text
    assert (
        "[completed] Requirements: 2 user stories, 3 acceptance criteria, 1 open questions" in text
    )
    event = next(e for e in events if e["event"] == "agent_completed")
    assert event["summary"] in text and event["duration_ms"] >= 0
    assert event["duration_seconds"] >= 0 and event["retry_attempt"] == 0
    assert event["run_id"] == RUN and event["timestamp"]
    assert event["artifact"].endswith("requirements.json")


def test_planning_summary_uses_output_not_stale_input(tmp_path):
    before = {"plan": {"tasks": [{}]}}
    output = {
        "status": "planning_complete",
        "plan": {
            "tasks": [
                {"dependencies": []},
                {"dependencies": ["T1"]},
                {"dependencies": ["T1", "T2"]},
            ],
            "blocked_task_ids": ["T2", "T3"],
        },
    }
    _, text, _ = observed(tmp_path, "planning", before, output)
    assert "3 tasks, 3 dependencies, DAG valid; 2 blocked" in text


def test_developer_identity_and_file_count(tmp_path):
    before = {
        "active_task": "TASK-03",
        "plan": {
            "tasks": [
                {"task_id": "TASK-01"},
                {"task_id": "TASK-02"},
                {"task_id": "TASK-03", "title": "Implement URL creation"},
            ]
        },
    }
    output = {
        "active_task": None,
        "development_results": [
            {
                "task_id": "TASK-03",
                "status": "completed",
                "generated_files": ["api.py", "models.py"],
            }
        ],
    }
    _, text, events = observed(tmp_path, "developer", before, output)
    assert "[3/3] TASK-03 Implement URL creation" in text and "2 files persisted" in text
    assert "Implementing task" not in text
    assert next(e for e in events if e["event"] == "agent_completed")["task_id"] == "TASK-03"


@pytest.mark.parametrize(
    "stage",
    [
        "requirements",
        "design",
        "design_validator",
        "planning",
        "developer",
        "qa",
        "review",
        "validation_gate",
        "validation_snapshot",
        "delivery",
    ],
)
def test_missing_metrics_are_safe(stage):
    assert isinstance(stage_summary(stage, {}, {}), str)
    nullable = {
        "requirements": {"user_stories": None, "open_questions": None},
        "design": {"components": None, "api_endpoints": None},
        "plan": {"tasks": None, "blocked_task_ids": None},
        "validation_files": None,
    }
    assert isinstance(stage_summary(stage, nullable, {}), str)


def test_design_and_parallel_validation_metrics():
    design = stage_summary(
        "design_validator",
        {},
        {
            "design": {"components": [{}, {}], "api_endpoints": [{}]},
            "design_validation_status": "valid",
            "design_retry_count": 1,
        },
    )
    assert "2 components, 1 API endpoints" in design and "attempt=2" in design
    state = {
        "validation_revision": REVISION,
        "qa_repair_count": 1,
        "qa_result": {"status": "qa_passed", "revision": REVISION, "retry_attempt": 1},
        "review_result": {
            "status": "approved",
            "reviewed_revision": REVISION,
            "retry_attempt": 1,
            "blocking_findings_count": 1,
            "findings": [{}, {}, {}],
        },
    }
    summary = stage_summary("validation_gate", state, {})
    assert "QA=qa_passed, Review=approved" in summary and "revision=consistent" in summary
    assert "repair_attempt=1" in summary
    review = stage_summary("review", state, {})
    assert "1 blocking, 2 nonblocking" in review and REVISION in review
    state["review_result"]["retry_attempt"] = 0
    assert "mismatched" in stage_summary("validation_gate", state, {})


def test_qa_command_and_actual_counts(tmp_path):
    qa = {
        "status": "qa_passed",
        "total_tests": 14,
        "passed_tests": 14,
        "failed_tests": 0,
        "skipped_tests": 0,
        "command": "/qa/bin/python -m pytest -q",
        "exit_code": 0,
    }
    _, text, _ = observed(tmp_path, "qa", {}, {"qa_result": qa})
    assert "14 tests: 14 passed, 0 failed, 0 skipped" in text
    assert "/qa/bin/python -m pytest -q; exit=0" in text
    assert "pytest not executed" in stage_summary("qa", {}, {"qa_result": {"status": "qa_blocked"}})


def test_precise_plan_failure_and_no_false_cause(tmp_path):
    reason = "Plan validation failed: Task T-05: dependency T-09 refers to a missing task."
    _, text, events = observed(
        tmp_path, "planning", {"planning_retry_count": 2}, {"status": "failed", "error": reason}
    )
    assert "invalid_task_plan" in text and "T-05" in text and "T-09" in text
    assert "retry=2" in text and str(tmp_path / "execution.log") in text
    event = next(e for e in events if e["event"] == "agent_failed")
    assert event["error_type"] == "invalid_task_plan" and event["error_message"] == reason
    assert "No diagnostic reason" in error_details("requirements", {})[1]


def test_secrets_and_terminal_controls_removed_from_summaries(tmp_path, monkeypatch):
    secret = 'SENSITIVE_FRAGMENT"value\nline'
    monkeypatch.setenv("SERVICE_PASSWORD", secret)
    before = {
        "active_task": "T1",
        "plan": {"tasks": [{"task_id": "T1", "title": f"Task {secret} \x1b[31mred\x1b[0m"}]},
    }
    _, terminal, _ = observed(
        tmp_path,
        "developer",
        before,
        {
            "development_results": [
                {"status": "failed", "error": f"failure {secret} token=hidden-token"}
            ]
        },
    )
    for text in (
        terminal,
        (tmp_path / "execution.log").read_text(),
        (tmp_path / "events.jsonl").read_text(),
    ):
        assert (
            "SENSITIVE_FRAGMENT" not in text and "hidden-token" not in text and "\x1b" not in text
        )
    assert "[REDACTED]" in terminal


def test_safe_exception_message_and_frames(tmp_path, monkeypatch):
    monkeypatch.setenv("SERVICE_TOKEN", "private-service-token")
    stream = io.StringIO()
    observer = RunObserver(RUN, tmp_path, ProgressRenderer(stream, ansi=False))

    def broken(state):
        raise OSError("Cannot open storage: private-service-token")

    with pytest.raises(OSError):
        observe_node("planning", broken)({"run_id": RUN})
    observer.close()
    output = stream.getvalue()
    assert "OSError: Cannot open storage: [REDACTED]" in output
    event = next(
        json.loads(line)
        for line in (tmp_path / "events.jsonl").read_text().splitlines()
        if json.loads(line)["event"] == "exception"
    )
    assert "broken" in event["stack_trace"] and "private-service-token" not in json.dumps(event)
    assert event["error_message"] == "Cannot open storage: [REDACTED]"
    assert event["duration_ms"] >= 0 and event["retry_attempt"] == 0
    assert event["status"] == "failed"


def test_approval_summary_has_exact_supported_command():
    import run

    state = {
        "status": "awaiting_approval",
        "run_id": RUN,
        "approval_request": {"revision": REVISION},
    }
    summary = stage_summary("approval_prepare", state, {})
    command = summary[summary.index("python run.py ") + len("python run.py ") :]
    args = run.parser().parse_args(command.split())
    assert args.command == "decide" and args.run_id == RUN and args.revision == REVISION


def test_real_cli_summary_fixture(tmp_path, capsys, monkeypatch, llm_response):
    import run

    monkeypatch.setattr(run, "RUNS_DIR", tmp_path / "runs")
    assert (
        run.main(
            ["start", "--requirement", "Build a URL shortener", "--stop-after", "requirements"]
        )
        == 0
    )
    capture = capsys.readouterr()
    assert "[completed] Project: mode=greenfield; repository=not supplied" in capture.err
    assert (
        "[completed] Requirements: 1 user story, 1 acceptance criterion, 0 open questions"
        in capture.err
    )
    assert "user_stories" not in capture.out
    print(
        capture.err, end=""
    )  # Visible with pytest -s for a reproducible offline CLI demonstration.


def test_human_interrupt_displays_question_and_resume(
    tmp_path, capsys, monkeypatch, llm_response, payload
):
    import run

    monkeypatch.setattr(run, "RUNS_DIR", tmp_path / "runs")
    payload.update(requires_human_clarification=True, open_questions=["Who can create links?"])
    assert run.main(["start", "--requirement", "Build a URL shortener"]) == 0
    captured = capsys.readouterr()
    assert "[waiting] Requirements:" in captured.err and "Who can create links?" in captured.err
    run_id = next(run.RUNS_DIR.iterdir()).name
    assert f"python run.py resume --run-id {run_id} --answers" in captured.err
    assert "paused" in captured.out


def test_malformed_optional_metrics_do_not_change_node_result(tmp_path):
    output = {"status": "design_generated", "design_candidate": {"components": 5}}
    result, text, _ = observed(tmp_path, "design", {}, output)
    assert result is output and "Structured metrics unavailable" in text


def test_stdout_failure_summary_redacts_known_secrets(tmp_path, capsys, monkeypatch):
    import run

    monkeypatch.setenv("DATABASE_PASSWORD", "unsafe-password-value")
    run.print_summary(
        {
            "run_id": RUN,
            "status": "failed",
            "error": "Cannot connect: unsafe-password-value",
            "failed_stage": "planning",
        }
    )
    text = capsys.readouterr().out
    assert "unsafe-password-value" not in text and "[REDACTED]" in text
    assert "Failed stage: planning" in text and "execution.log" in text
