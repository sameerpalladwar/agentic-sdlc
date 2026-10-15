"""Scheduling and package validation are independent gates; generated code is never executed."""

import copy

import pytest
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.types import Command

from models.developer import DeveloperOutput
from orchestrator import file_writer
from orchestrator.dependencies import FASTAPI_PINS
from orchestrator.development import developer_agent, prepare_development, select_task
from orchestrator.graph import build_graph
from tests.conftest import approved_review
from tests.test_developer import RUN_ID, initial, output


def task(name, dependencies, path=None):
    return {
        "task_id": name,
        "title": name,
        "description": "Implement assigned artifacts",
        "related_user_story_ids": ["US-001"],
        "dependencies": dependencies,
        "expected_artifacts": ["workspace/output_code/" + (path or name.lower() + ".py")],
        "acceptance_checks": ["Assigned artifacts provide required behavior"],
        "status": "pending",
    }


@pytest.fixture(autouse=True)
def isolated_output(tmp_path, monkeypatch):
    monkeypatch.setattr(file_writer, "OUTPUT_ROOT", tmp_path / "output")


def state_for(payload, design_payload, tasks):
    return initial(payload, design_payload, {"tasks": tasks, "shared_artifacts": []})


def generated(context):
    return DeveloperOutput.model_validate(output(context["task"]))


@pytest.mark.parametrize(
    "edges",
    [
        [("T1", []), ("T2", ["T1"]), ("T3", ["T2"])],
        [("T1", []), ("T2", ["T1"]), ("T3", ["T1"]), ("T4", ["T2", "T3"])],
    ],
)
def test_linear_and_branching_order(edges, payload, design_payload, monkeypatch):
    state = state_for(payload, design_payload, [task(*edge) for edge in edges])
    monkeypatch.setattr("orchestrator.development.generate_task", generated)
    state.update(prepare_development(state))
    order = []
    for _ in edges:
        state.update(select_task(state))
        current = next(t for t in state["plan"]["tasks"] if t["task_id"] == state["active_task"])
        assert all(dep in order for dep in current["dependencies"])
        order.append(current["task_id"])
        state.update(developer_agent(state))
        assert state["development_results"][-1]["status"] == "completed"
        assert all(
            (file_writer.OUTPUT_ROOT / RUN_ID / p.removeprefix("workspace/output_code/")).is_file()
            for p in current["expected_artifacts"]
        )
    state.update(select_task(state))
    assert state["status"] == "development_complete" and not state["active_task"]
    assert order == [name for name, _ in edges]


@pytest.mark.parametrize("problem", ["missing", "duplicate", "self", "cycle"])
def test_invalid_dag_rejected_before_generation(problem, payload, design_payload, isolated_llm):
    state = state_for(payload, design_payload, [task("T1", []), task("T2", ["T1"])])
    tasks = state["plan"]["tasks"]
    if problem == "missing":
        tasks[1]["dependencies"] = ["MISSING"]
    elif problem == "duplicate":
        tasks[1]["task_id"] = "T1"
    elif problem == "self":
        tasks[1]["dependencies"] = ["T2"]
    else:
        tasks[0]["dependencies"] = ["T2"]
    for handler in (prepare_development, select_task):
        result = handler(state)
        assert result["status"] == "failed"
        assert result["development_issues"][0]["code"] == "invalid_task_plan"
        assert "T1" in result["error"] or "T2" in result["error"]
        if problem == "missing":
            assert "MISSING" in result["error"]
    isolated_llm.responses.parse.assert_not_called()
    assert not file_writer.OUTPUT_ROOT.exists()


def test_failed_prerequisite_reports_status(payload, design_payload):
    state = state_for(payload, design_payload, [task("T1", []), task("T2", ["T1"])])
    state["plan"]["tasks"][0]["status"] = "failed"
    result = select_task(state)
    assert result["status"] == "development_paused" and not result["active_task"]
    issue = next(i for i in result["development_issues"] if i["task_id"] == "T2")
    assert issue["unmet_dependency_ids"] == ["T1"]
    assert issue["dependency_statuses"] == {"T1": "failed"}
    assert "T1" in result["error"] and "failed" in result["error"]


def test_completed_claim_requires_persisted_artifact(payload, design_payload):
    state = state_for(payload, design_payload, [task("T1", [])])
    state["plan"]["tasks"][0]["status"] = "completed"
    result = select_task(state)
    assert result["status"] == "failed"
    assert result["development_issues"][0]["code"] == "missing_task_artifact"
    assert result["development_issues"][0]["task_id"] == "T1"


def test_completed_checkpoint_restarts_into_parallel_validation(
    payload, design_payload, tmp_path, monkeypatch
):
    state = state_for(payload, design_payload, [task("T1", []), task("T2", ["T1"])])
    state["through"] = "delivery"
    monkeypatch.setattr("orchestrator.development.generate_task", generated)
    monkeypatch.setattr("agents.reviewer.generate_review", approved_review)
    state.update(prepare_development(state))
    config = {"configurable": {"thread_id": RUN_ID}}
    database = str(tmp_path / "checkpoints.sqlite")
    with SqliteSaver.from_conn_string(database) as saver:
        graph = build_graph(saver)
        # Simulate a process exit after T1's persistence and completion checkpoint.
        state.update(select_task(state))
        state.update(developer_agent(state))
        graph.update_state(config, state, as_node="developer")
    seen = []

    def track(context):
        seen.append(context["task"]["task_id"])
        return generated(context)

    monkeypatch.setattr("orchestrator.development.generate_task", track)
    with SqliteSaver.from_conn_string(database) as saver:
        result = build_graph(saver).invoke(None, config)
    assert seen == ["T2"]  # T1 is never regenerated after restart.
    assert all(t["status"] == "completed" for t in result["plan"]["tasks"])
    assert result["status"] == "validation_blocked"  # Local execution was not authorized.
    assert result["qa_result"]["status"] == "qa_blocked"
    assert result["review_result"]["status"] == "approved"
    assert result["qa_result"]["revision"] == result["review_result"]["reviewed_revision"]


def test_missing_sqlalchemy_is_rejected_before_final_task_persistence(
    payload, design_payload, monkeypatch, tmp_path
):
    """Reproduce the run's SQLAlchemy imports and baseline-only requirements, without execution."""
    source = task("TASK-001", [], "app/database.py")
    packaging = task("TASK-008", ["TASK-001"], "requirements.txt")
    packaging["expected_artifacts"].append("workspace/output_code/README.md")
    state = state_for(payload, design_payload, [source, packaging])
    requirements = "\n".join(f"{name}=={version}" for name, version in FASTAPI_PINS.items())

    def generate(context):
        result = output(context["task"])
        for file in result["files"]:
            if file["path"].endswith(".py"):
                file["content"] = "from sqlalchemy import create_engine\n"
            elif file["path"].endswith("README.md"):
                file["content"] = "Python 3.12 application"
            else:
                file["content"] = requirements
        return DeveloperOutput.model_validate(result)

    monkeypatch.setattr("orchestrator.development.generate_task", generate)
    state.update(prepare_development(state))
    state.update(select_task(state))
    state.update(developer_agent(state))
    original = (file_writer.OUTPUT_ROOT / RUN_ID / "app/database.py").read_bytes()
    state.update(select_task(state))
    state.update(developer_agent(state))
    assert state["plan"]["tasks"][1]["status"] == "failed"
    issue = state["development_results"][-1]["issues"][0]
    assert issue["code"] == "missing_dependency" and issue["task_id"] == "TASK-008"
    assert "sqlalchemy" in issue["message"]
    assert not (file_writer.OUTPUT_ROOT / RUN_ID / "requirements.txt").exists()
    assert (file_writer.OUTPUT_ROOT / RUN_ID / "app/database.py").read_bytes() == original
    state.update(select_task(state))
    assert state["status"] == "development_paused" and "sqlalchemy" in state["error"]
    # A terminal all-completed state from an older run must still fail the final gate.
    legacy = copy.deepcopy(state)
    legacy["plan"]["tasks"][1]["status"] = "completed"
    legacy["run_id"] = "b" * 32
    with file_writer.SafeWorkspace(legacy["run_id"]) as workspace:
        workspace.persist(generate({"task": source}), "legacy-source-fixture")
        workspace.persist(generate({"task": packaging}), "legacy-package-fixture")
    result = select_task(legacy)
    assert result["status"] == "failed"
    assert "package/runtime" in result["error"] and "sqlalchemy" in result["error"]

    # Use the existing human-triggered bounded task retry; no new autonomous loop.
    database = str(tmp_path / "retry.sqlite")
    config = {"configurable": {"thread_id": RUN_ID}}
    state["through"] = "delivery"
    monkeypatch.setattr("agents.reviewer.generate_review", approved_review)
    with SqliteSaver.from_conn_string(database) as saver:
        graph = build_graph(saver)
        graph.update_state(config, state, as_node="developer")
        paused = graph.invoke(None, config)
        assert paused["__interrupt__"] and paused["status"] == "development_paused"
    requirements += "\nsqlalchemy==2.0.0"  # Static fixture pin only; never installed or executed.
    attempts = []

    def corrected(context):
        attempts.append(context)
        return generate(context)

    monkeypatch.setattr("orchestrator.development.generate_task", corrected)
    with SqliteSaver.from_conn_string(database) as saver:
        result = build_graph(saver).invoke(Command(resume={"retry_development": True}), config)
    assert len(attempts) == 1 and attempts[0]["task"]["task_id"] == "TASK-008"
    assert "sqlalchemy" in attempts[0]["previous_attempts"][0]["issues"][0]["message"]
    assert result["development_results"][-1]["attempt"] == 2
    assert all(t["status"] == "completed" for t in result["plan"]["tasks"])
    assert result["qa_result"]["status"] == "qa_blocked"
    assert result["review_result"]["status"] == "approved"
    assert result["status"] == "validation_blocked"
