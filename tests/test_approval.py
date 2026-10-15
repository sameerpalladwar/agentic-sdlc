import json

import pytest
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.types import Command

from models.developer import DeveloperOutput
from orchestrator.file_writer import SafeWorkspace
from orchestrator.graph import build_graph
from tests.test_qa_recovery import RUN, execute
from tests.test_validation import passed


def decision(state, action="approve", **extra):
    request = state["approval_request"]
    return {
        "decision": action,
        "revision": request["revision"],
        "request_id": request["request_id"],
        **extra,
    }


def await_approval(ready, tmp_path, monkeypatch):
    ready["through"] = "delivery"
    monkeypatch.setattr("agents.qa.run_tests", lambda *a, **k: passed())
    return execute(ready, tmp_path)[0]


def continue_run(tmp_path, choice):
    with SqliteSaver.from_conn_string(str(tmp_path / "checkpoint.sqlite")) as saver:
        graph = build_graph(saver)
        return graph.invoke(Command(resume=choice), {"configurable": {"thread_id": RUN}})


def test_approval_and_delivery(ready, tmp_path, monkeypatch):
    state = await_approval(ready, tmp_path, monkeypatch)
    assert state["status"] == "awaiting_approval"
    assert state["approval_request"]["kind"] == "approval"
    with SafeWorkspace(RUN) as workspace:
        with pytest.raises(ValueError, match="approval"):
            workspace.persist(
                DeveloperOutput(
                    task_id="bad",
                    files=[],
                    implementation_summary="No changes",
                    acceptance_check_mapping=[{"check": "none", "implementation": "none"}],
                    assumptions=[],
                    warnings=[],
                    completion_status="completed",
                ),
                "bad",
            )
    result = continue_run(tmp_path, decision(state))
    assert result["status"] == "completed"
    assert result["final_manifest"]["approved_revision"] == state["validation_revision"]
    assert result["final_manifest"]["human_approval"]["decision"] == "approve"
    assert result["final_manifest"]["known_limitations"]
    assert result["qa_result"]["revision"] == result["review_result"]["reviewed_revision"]
    with SafeWorkspace(RUN) as workspace:
        assert workspace.manifest["delivery_revision"] == state["validation_revision"]


def test_reject(ready, tmp_path, monkeypatch):
    state = await_approval(ready, tmp_path, monkeypatch)
    result = continue_run(tmp_path, decision(state, "reject", feedback="Not ready"))
    assert result["status"] == "rejected"
    assert result["approval_history"][0]["feedback"] == "Not ready"
    assert not result.get("final_manifest")


@pytest.mark.parametrize("change", ["revision", "source", "review"])
def test_stale_approval(ready, tmp_path, monkeypatch, change):
    state = await_approval(ready, tmp_path, monkeypatch)
    choice = decision(state)
    if change == "revision":
        choice["revision"] = "stale"
    if change == "source":
        from orchestrator import file_writer

        (file_writer.OUTPUT_ROOT / RUN / "application.py").write_text("changed=1")
    if change == "review":
        with SqliteSaver.from_conn_string(str(tmp_path / "checkpoint.sqlite")) as saver:
            graph = build_graph(saver)
            graph.update_state(
                {"configurable": {"thread_id": RUN}},
                {"review_result": {**state["review_result"], "status": "changes_requested"}},
                as_node="approval_prepare",
            )
    result = continue_run(tmp_path, choice)
    assert result["status"] == "approval_blocked"
    assert not result.get("final_manifest")


def test_request_changes_revalidates_and_requests_new_approval(ready, tmp_path, monkeypatch):
    from tests.test_qa_recovery import repair

    state = await_approval(ready, tmp_path, monkeypatch)
    contexts = []

    def generate(context):
        contexts.append(context)
        result = repair()
        result.task_id = "human-repair-1"
        return result

    monkeypatch.setattr("orchestrator.qa_recovery.generate_task", generate)
    updated = continue_run(
        tmp_path,
        decision(
            state,
            "request_changes",
            feedback="Return one from application.py",
            files=["workspace/output_code/application.py"],
        ),
    )
    assert updated["status"] == "awaiting_approval"
    assert updated["validation_revision"] != state["validation_revision"]
    assert updated["approval_request"]["request_id"] != state["approval_request"]["request_id"]
    assert contexts[0]["mode"] == "human_repair"
    assert updated.get("qa_repair_count", 0) == 0
    assert updated["qa_result"]["revision"] == updated["review_result"]["reviewed_revision"]
    assert updated["qa_result"]["retry_attempt"] == 0
    assert len(updated["approval_history"]) == 1
    delivered = continue_run(tmp_path, decision(updated))
    assert delivered["status"] == "completed" and len(delivered["approval_history"]) == 2


def test_cli_approval_restart_duplicate_and_exports(ready, tmp_path, monkeypatch, capsys):
    import run

    monkeypatch.setattr(run, "RUNS_DIR", tmp_path / "runs")
    root = run.RUNS_DIR / RUN
    root.mkdir(parents=True)
    monkeypatch.setattr("agents.qa.run_tests", lambda *a, **k: passed())
    with SqliteSaver.from_conn_string(str(root / "checkpoints.sqlite")) as saver:
        build_graph(saver).update_state(
            {"configurable": {"thread_id": RUN}}, ready, as_node="development_select"
        )
    assert (
        run.main(
            [
                "qa",
                "--run-id",
                RUN,
                "--stop-after",
                "delivery",
                "--authorize-local-execution",
                "--json",
            ]
        )
        == 0
    )
    pending = json.loads(capsys.readouterr().out)
    assert pending["status"] == "paused" and pending["pause"]["kind"] == "approval"
    args = [
        "decide",
        "--run-id",
        RUN,
        "--revision",
        pending["validation_revision"],
        "--decision",
        "approve",
        "--json",
    ]
    assert run.main(args) == 0
    completed = json.loads(capsys.readouterr().out)
    assert completed["status"] == "completed"
    manifest = json.loads((root / "final_manifest.json").read_text())
    assert manifest == completed["final_manifest"]
    assert run.main(args) == 1
    capsys.readouterr()
    # Completed-state replay exports the same manifest without another approval.
    assert run.main(["resume", "--run-id", RUN, "--retry-development", "--json"]) == 0
    replay = json.loads(capsys.readouterr().out)
    assert replay["final_manifest"] == manifest


def test_cli_invalid_feedback_keeps_interrupt(ready, tmp_path, monkeypatch, capsys):
    import run

    monkeypatch.setattr(run, "RUNS_DIR", tmp_path / "runs")
    root = run.RUNS_DIR / RUN
    root.mkdir(parents=True)
    monkeypatch.setattr("agents.qa.run_tests", lambda *a, **k: passed())
    with SqliteSaver.from_conn_string(str(root / "checkpoints.sqlite")) as saver:
        build_graph(saver).update_state(
            {"configurable": {"thread_id": RUN}}, ready, as_node="development_select"
        )
    run.main(
        ["qa", "--run-id", RUN, "--stop-after", "delivery", "--authorize-local-execution", "--json"]
    )
    pending = json.loads(capsys.readouterr().out)
    assert (
        run.main(
            [
                "decide",
                "--run-id",
                RUN,
                "--revision",
                pending["validation_revision"],
                "--decision",
                "request_changes",
                "--json",
            ]
        )
        == 1
    )
    capsys.readouterr()
    with SqliteSaver.from_conn_string(str(root / "checkpoints.sqlite")) as saver:
        saved = build_graph(saver).get_state({"configurable": {"thread_id": RUN}})
        assert saved.values["status"] == "awaiting_approval"
        assert any(task.interrupts for task in saved.tasks)


def test_journal_recovers_crash_before_decision_checkpoint(ready, tmp_path, monkeypatch):
    import orchestrator.approval as approval

    state = await_approval(ready, tmp_path, monkeypatch)
    original = approval.decision_update
    monkeypatch.setattr(
        approval,
        "decision_update",
        lambda *a: (_ for _ in ()).throw(RuntimeError("simulated crash")),
    )
    with pytest.raises(RuntimeError):
        continue_run(tmp_path, decision(state))
    monkeypatch.setattr(approval, "decision_update", original)
    result = continue_run(tmp_path, decision(state))
    assert result["status"] == "completed"
    assert len(result["approval_history"]) == 1


def test_demo_real_qa_approval_delivery(ready, tmp_path, monkeypatch, capsys):
    """A trusted handwritten fixture executes real pytest; review is mocked."""
    import sys
    from pathlib import Path

    import run
    from tests.test_qa_recovery import repair

    monkeypatch.setattr("orchestrator.qa_runner.qa_python", lambda run_id: Path(sys.executable))
    with SafeWorkspace(RUN) as workspace:
        workspace.persist(repair(), "trusted-good-fixture")
    monkeypatch.setattr(run, "RUNS_DIR", tmp_path / "runs")
    root = run.RUNS_DIR / RUN
    root.mkdir(parents=True)
    with SqliteSaver.from_conn_string(str(root / "checkpoints.sqlite")) as saver:
        build_graph(saver).update_state(
            {"configurable": {"thread_id": RUN}}, ready, as_node="development_select"
        )
    assert (
        run.main(
            [
                "qa",
                "--run-id",
                RUN,
                "--stop-after",
                "delivery",
                "--authorize-local-execution",
                "--verbose",
            ]
        )
        == 0
    )
    pending = capsys.readouterr()
    request = json.loads((root / "approval_request.json").read_text())
    assert (
        f"python run.py decide --run-id {RUN} --revision {request['revision']} --decision approve"
        in pending.out
    )
    assert "paused" in pending.out
    assert (
        run.main(
            ["decide", "--run-id", RUN, "--revision", request["revision"], "--decision", "approve"]
        )
        == 0
    )
    final = capsys.readouterr()
    manifest = json.loads((root / "final_manifest.json").read_text())
    assert manifest["qa_summary"]["passed_tests"] == 1
    assert manifest["approved_revision"] == request["revision"]
    events = [json.loads(line) for line in (root / "events.jsonl").read_text().splitlines()]
    assert sum(event["event"] == "human_approval_resume" for event in events) == 1
    assert any(event["event"] == "parallel_started" and event["stage"] == "qa" for event in events)
    assert any(
        event["event"] == "parallel_completed" and event["stage"] == "review" for event in events
    )
    print("\nSAMPLE TERMINAL:\n" + pending.err + pending.out + final.err + final.out)
    print("SAMPLE_RUN_DIRECTORY:", root)


def test_unsafe_file_feedback_keeps_pending_approval(ready, tmp_path, monkeypatch, capsys):
    import run

    monkeypatch.setattr(run, "RUNS_DIR", tmp_path / "runs")
    root = run.RUNS_DIR / RUN
    root.mkdir(parents=True)
    monkeypatch.setattr("agents.qa.run_tests", lambda *a, **k: passed())
    with SqliteSaver.from_conn_string(str(root / "checkpoints.sqlite")) as saver:
        build_graph(saver).update_state(
            {"configurable": {"thread_id": RUN}}, ready, as_node="development_select"
        )
    run.main(
        ["qa", "--run-id", RUN, "--stop-after", "delivery", "--authorize-local-execution", "--json"]
    )
    pending = json.loads(capsys.readouterr().out)
    assert (
        run.main(
            [
                "decide",
                "--run-id",
                RUN,
                "--revision",
                pending["validation_revision"],
                "--decision",
                "request_changes",
                "--feedback",
                "Change tests",
                "--file",
                "workspace/output_code/test_app.py",
                "--json",
            ]
        )
        == 1
    )
    capsys.readouterr()
    with SqliteSaver.from_conn_string(str(root / "checkpoints.sqlite")) as saver:
        saved = build_graph(saver).get_state({"configurable": {"thread_id": RUN}})
        assert saved.values["status"] == "awaiting_approval"


def test_reject_stops_even_after_source_changes(ready, tmp_path, monkeypatch):
    from orchestrator import file_writer

    state = await_approval(ready, tmp_path, monkeypatch)
    (file_writer.OUTPUT_ROOT / RUN / "application.py").write_text("changed=1")
    result = continue_run(tmp_path, decision(state, "reject", feedback="Reject modified source"))
    assert result["status"] == "rejected"
    assert result["approval_history"][-1]["decision"] == "reject"
    assert not result.get("final_manifest")
