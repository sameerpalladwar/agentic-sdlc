import io
import json

import pytest

from orchestrator.observability import OBSERVERS, RunObserver, observe_node
from orchestrator.progress import ProgressRenderer

RUN = "c" * 32


def observer(tmp_path, **kwargs):
    return RunObserver(RUN, tmp_path, ProgressRenderer(io.StringIO(), **kwargs))


def events(path):
    return [json.loads(line) for line in (path / "events.jsonl").read_text().splitlines()]


def test_events_and_redaction(tmp_path, monkeypatch):
    key = "sk-proj-" + "A" * 35
    monkeypatch.setenv("OPENAI_API_KEY", key)
    monkeypatch.setenv("DATABASE_PASSWORD", "local-password")
    log = observer(tmp_path)
    log.emit("test", stage=f"{key} local-password Bearer sensitive-token", payload="raw LLM body")
    log.close()
    text = (tmp_path / "events.jsonl").read_text() + (tmp_path / "execution.log").read_text()
    assert key not in text and "local-password" not in text and "sensitive-token" not in text
    assert "raw LLM" not in text
    event = events(tmp_path)[0]
    assert event["run_id"] == RUN and event["severity"] == "INFO" and event["timestamp"]


def test_resume_does_not_duplicate_handlers(tmp_path):
    for _ in range(2):
        log = observer(tmp_path)
        assert len(log.logger.handlers) == 2
        log.emit("resume", stage="workflow")
        log.close()
        assert not log.logger.handlers
    assert len(events(tmp_path)) == 2
    assert RUN not in OBSERVERS


def test_logging_io_failure_is_nonfatal(tmp_path, monkeypatch):
    log = observer(tmp_path)

    def unavailable(*args, **kwargs):
        raise OSError("disk unavailable")

    for handler in log.logger.handlers:
        monkeypatch.setattr(handler.stream, "write", unavailable)
    log.emit("safe", stage="workflow")
    log.close()


def test_unsafe_log_destination_is_not_followed(tmp_path):
    target = tmp_path / "outside"
    target.write_text("unchanged")
    (tmp_path / "execution.log").symlink_to(target)
    log = observer(tmp_path)
    log.emit("test", stage="workflow")
    log.close()
    assert target.read_text() == "unchanged"


def test_node_exception_has_redacted_stack(tmp_path, monkeypatch):
    secret = "my-private-token"
    monkeypatch.setenv("SERVICE_TOKEN", secret)
    log = observer(tmp_path)

    def broken(state):
        raise RuntimeError(secret)

    with pytest.raises(RuntimeError):
        observe_node("developer", broken)({"run_id": RUN})
    log.close()
    event = next(item for item in events(tmp_path) if item["event"] == "exception")
    assert event["error_type"] == "RuntimeError" and "broken" in event["stack_trace"]
    assert secret not in (tmp_path / "events.jsonl").read_text()


def test_progress_plain_and_parallel_formatting():
    stream = io.StringIO()
    progress = ProgressRenderer(stream, ansi=False)
    progress.render("qa", "running")
    progress.render("review", "running")
    progress.render("qa", "completed")
    progress.render("review", "completed")
    progress.close()
    text = stream.getvalue()
    assert "\033" not in text and "[running] QA:" in text
    assert "[completed] Code Review:" in text


def test_progress_ansi_keeps_completed_summaries():
    stream = io.StringIO()
    progress = ProgressRenderer(stream, ansi=True)
    progress.render("qa", "running")
    progress.render("review", "running")
    progress.render("qa", "completed")
    progress.render("review", "completed")
    progress.close()
    text = stream.getvalue()
    assert " | " in text
    assert "[completed] QA:" in text and "[completed] Code Review:" in text


def test_progress_disabled():
    stream = io.StringIO()
    progress = ProgressRenderer(stream, enabled=False)
    progress.render("qa", "running")
    progress.render("qa", "completed")
    progress.close()
    assert stream.getvalue() == ""


def test_cli_default_is_compact_and_verbose_is_safe(tmp_path, monkeypatch, capsys, llm_response):
    import run

    monkeypatch.setattr(run, "RUNS_DIR", tmp_path / "runs")
    assert run.main(["--verbose", "--requirement", "Shorten URLs"]) == 0
    captured = capsys.readouterr()
    assert captured.out.startswith("Run ") and "Artifacts:" in captured.out
    assert "user_stories" not in captured.out
    assert "Requirements:" in captured.err and "s)" in captured.err
    root = next(run.RUNS_DIR.iterdir())
    names = [event["event"] for event in events(root)]
    for name in [
        "run_started",
        "agent_started",
        "agent_completed",
        "transition",
        "artifact_created",
        "run_completed",
    ]:
        assert name in names


def test_developer_task_failure_is_logged(tmp_path):
    log = observer(tmp_path)

    def function(state):
        return {"development_results": [{"status": "failed"}]}

    observe_node("developer", function)({"run_id": RUN})
    log.close()
    assert any(event["event"] == "agent_failed" for event in events(tmp_path))


def test_logging_unavailable_still_completes(tmp_path, monkeypatch, capsys, llm_response):
    import orchestrator.observability as module
    import run

    monkeypatch.setattr(run, "RUNS_DIR", tmp_path / "runs")
    monkeypatch.setattr(
        module,
        "SafeFileHandler",
        lambda *a, **k: (_ for _ in ()).throw(OSError("No logging storage")),
    )
    assert run.main(["--requirement", "Shorten URLs", "--json"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["status"] == "requirements_complete"


def test_retry_exhaustion_is_observable(tmp_path):
    log = observer(tmp_path)

    def exhausted(state):
        return {
            "status": "validation_failed",
            "failure_reason": "Automatic repair budget exhausted.",
        }

    observe_node("validation_finish", exhausted)({"run_id": RUN, "qa_repair_count": 2})
    log.close()
    event = next(event for event in events(tmp_path) if event["event"] == "retry_exhausted")
    assert event["attempt"] == 2


def test_quoted_and_multiline_secrets_are_redacted(tmp_path, monkeypatch):
    secret = 'sensitive"quote\nline'
    monkeypatch.setenv("SERVICE_SECRET", secret)
    log = observer(tmp_path)
    log.emit("test", stage=secret, status={"raw": "nested model payload"})
    log.close()
    event = events(tmp_path)[0]
    assert event["stage"] == "[REDACTED]" and event["status"] == "[OMITTED]"
    assert "sensitive" not in (tmp_path / "events.jsonl").read_text()
