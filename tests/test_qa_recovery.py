import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from langgraph.checkpoint.sqlite import SqliteSaver

from models.developer import DeveloperOutput
from models.qa import QAResult
from orchestrator.file_writer import SafeWorkspace
from orchestrator.graph import build_graph
from orchestrator.qa_recovery import preserve_guards

RUN = "b" * 32
BAD = "def value():\n    return 0\n"
GOOD = "def value():\n    return 1\n"
TEST = "from application import value\n\ndef test_value():\n    assert value() == 1\n"


def repair(content=GOOD, path="workspace/output_code/application.py", attempt=1):
    return DeveloperOutput(
        task_id=f"qa-repair-{attempt}",
        files=[{"path": path, "content": content + f"# repair attempt {attempt}\n"}],
        implementation_summary="Correct constant result",
        acceptance_check_mapping=[
            {"check": "value returns one", "implementation": "Return correct value"}
        ],
        assumptions=[],
        warnings=[],
        completion_status="completed",
    )


def fail():
    return QAResult(
        status="qa_failed",
        exit_code=1,
        total_tests=1,
        failed_tests=1,
        test_output="FAILED test_app.py::test_value - assert 0 == 1\n1 failed in 0.01s",
    )


def execute(state, tmp_path):
    with SqliteSaver.from_conn_string(str(tmp_path / "checkpoint.sqlite")) as saver:
        graph = build_graph(saver)
        config = {"configurable": {"thread_id": RUN}}
        graph.update_state(config, state, as_node="development_select")
        transitions = []
        for update in graph.stream(None, config, stream_mode="updates"):
            transitions.extend(update.keys())
        saved = graph.get_state(config)
        assert saved.values["run_id"] == RUN
        return saved.values, transitions


@pytest.mark.parametrize("attempts", [0, 1, 2, 3])
def test_repair_budget(ready, tmp_path, monkeypatch, attempts):
    import agents.qa as qa
    import orchestrator.qa_recovery as recovery

    reports = [fail() for _ in range(min(attempts, 3))]
    if attempts < 3:
        reports.append(QAResult(status="qa_passed", exit_code=0, total_tests=1, passed_tests=1))
    monkeypatch.setattr(qa, "run_tests", lambda *a, **k: reports.pop(0))
    calls = []

    def generate(context):
        calls.append(context)
        return repair(attempt=len(calls))

    monkeypatch.setattr(recovery, "generate_task", generate)
    result, transitions = execute(ready, tmp_path)
    assert result.get("qa_repair_count", 0) == min(attempts, 2)
    assert len(calls) == min(attempts, 2)
    assert result["status"] == ("validation_failed" if attempts == 3 else "validation_passed")
    assert result["recovery_history"][-1]["stage"] == "validation_final"
    assert transitions.count("qa") == min(attempts, 2) + 1
    if calls:
        assert calls[0]["requirements"] == ready["requirements"]
        assert calls[0]["qa_feedback"]["failing_tests"] == ["test_app.py::test_value"]
    if attempts == 3:
        assert "exhausted" in result["failure_reason"]


@pytest.mark.parametrize("blocked", ["approval", "execution"])
def test_nonretryable(ready, tmp_path, monkeypatch, blocked):
    import agents.qa as qa

    monkeypatch.setattr(
        "orchestrator.qa_recovery.generate_task", lambda c: pytest.fail("Must stop")
    )
    if blocked == "approval":
        ready["qa_local_authorized"] = False
        monkeypatch.setattr(
            qa,
            "run_tests",
            lambda *a, **k: QAResult(status="qa_blocked", error_summary="Approval missing"),
        )
    else:
        monkeypatch.setattr(
            qa, "run_tests", lambda *a, **k: QAResult(status="qa_failed", exit_code=2)
        )
    result, transitions = execute(ready, tmp_path)
    assert "developer_repair" not in transitions
    assert result["status"] != "qa_passed"


@pytest.mark.parametrize("kind", ["unsafe", "tests", "syntax", "llm", "guard"])
def test_repair_rejected_without_corruption(ready, tmp_path, monkeypatch, kind):
    import agents.qa as qa

    monkeypatch.setattr(qa, "run_tests", lambda *a, **k: fail())
    with SafeWorkspace(RUN) as w:
        original = w.snapshot()

    def generate(context):
        if kind == "llm":
            raise ValueError("OpenAI developer request failed.")
        if kind == "unsafe":
            return repair(path="../../escape.py")
        if kind == "tests":
            return repair(path="workspace/output_code/test_app.py")
        if kind == "syntax":
            return repair(content="invalid python !!")
        return repair(content="def value():\n    return 2\n")

    if kind == "guard":
        monkeypatch.setattr(
            "orchestrator.qa_recovery.preserve_guards",
            lambda *a: (_ for _ in ()).throw(ValueError("guard")),
        )
    monkeypatch.setattr("orchestrator.qa_recovery.generate_task", generate)
    result, transitions = execute(ready, tmp_path)
    assert result["status"] == "validation_failed"
    with SafeWorkspace(RUN) as w:
        assert w.snapshot() == original
    assert transitions.count("qa") == 1


def test_security_guard_retention():
    with pytest.raises(ValueError):
        preserve_guards(
            "def f(x):\n    if not x:\n        raise ValueError()\n    return x\n",
            "def f(x):\n    return x\n",
        )


@pytest.mark.parametrize("correct", [True, False])
def test_real_trusted_pytest_repair(ready, tmp_path, monkeypatch, correct):
    monkeypatch.setattr("orchestrator.qa_runner.qa_python", lambda run_id: Path(sys.executable))
    monkeypatch.setattr(
        "orchestrator.qa_recovery.generate_task",
        lambda context: repair(
            content=GOOD if correct else BAD, attempt=int(context["task_id"].rsplit("-", 1)[1])
        ),
    )
    result, transitions = execute(ready, tmp_path)
    assert result["status"] == ("validation_passed" if correct else "validation_failed")
    qa = [h for h in result["recovery_history"] if h.get("stage") == "qa"]
    assert [h["qa_results"]["failed_tests"] for h in qa] == ([1, 0] if correct else [1, 1, 1])
    assert qa[-1]["qa_results"]["passed_tests"] == int(correct)
    assert all(
        c["status"] == "not_verified" for c in result["qa_results"]["acceptance_criteria_results"]
    )
    print("\nTRANSITIONS:", " -> ".join(transitions))
    print("RECOVERY:", json.dumps(result["recovery_history"], indent=2))


def test_checkpoint_replay(ready, tmp_path, monkeypatch):
    import agents.qa as qa
    from orchestrator.qa_recovery import assess_qa, begin_repair

    monkeypatch.setattr(
        qa,
        "run_tests",
        lambda *a, **k: QAResult(status="qa_passed", exit_code=0, total_tests=1, passed_tests=1),
    )
    calls = []

    def generate(context):
        calls.append(context)
        return repair()

    monkeypatch.setattr("orchestrator.qa_recovery.generate_task", generate)
    ready.update(status="qa_failed", qa_results=fail().model_dump())
    ready.update(assess_qa(ready))
    ready.update(begin_repair(ready))
    with SqliteSaver.from_conn_string(str(tmp_path / "checkpoint.sqlite")) as saver:
        graph = build_graph(saver)
        config = {"configurable": {"thread_id": RUN}}
        graph.update_state(config, ready, as_node="repair_begin")
        # Reopen graph against the same durable checkpoint after interruption.
    with SqliteSaver.from_conn_string(str(tmp_path / "checkpoint.sqlite")) as saver:
        graph = build_graph(saver)
        result = graph.invoke(None, config)
    assert result["status"] == "validation_passed" and result["qa_repair_count"] == 1
    assert len(calls) == 1


def test_repair_uses_existing_sdk(ready, tmp_path, monkeypatch, isolated_llm):
    import agents.qa as qa

    reports = [fail(), QAResult(status="qa_passed", exit_code=0, total_tests=1, passed_tests=1)]
    monkeypatch.setattr(qa, "run_tests", lambda *a, **k: reports.pop(0))
    isolated_llm.responses.parse.return_value = SimpleNamespace(
        status="completed", output_parsed=repair().model_dump()
    )
    result, _ = execute(ready, tmp_path)
    assert result["status"] == "validation_passed"
    context = json.loads(isolated_llm.responses.parse.call_args.kwargs["input"][1]["content"])
    assert context["mode"] == "qa_repair" and context["task_id"] == "qa-repair-1"
    assert context["allowed_repair_paths"] == ["workspace/output_code/application.py"]
