import json
from types import SimpleNamespace

import pytest

import orchestrator.file_writer as writer
import run
from models.developer import DeveloperOutput
from models.planning import ExecutionPlan
from models.requirements import RequirementsOutput
from orchestrator.development import developer_agent, prepare_development, select_task
from orchestrator.task_plan import validate_execution_plan

RUN_ID = "a" * 32


@pytest.fixture(autouse=True)
def output_root(tmp_path, monkeypatch):
    monkeypatch.setattr(writer, "OUTPUT_ROOT", tmp_path / "output_code")
    monkeypatch.setattr(run, "RUNS_DIR", tmp_path / "runs")


@pytest.fixture
def developer_plan():
    def task(task_id, dependencies, file):
        return {
            "task_id": task_id,
            "title": task_id,
            "description": "Implement the task",
            "related_user_story_ids": ["US-001"],
            "dependencies": dependencies,
            "expected_artifacts": ["workspace/output_code/" + file],
            "acceptance_checks": ["Implementation provides the required behavior"],
            "status": "pending",
        }

    return {
        "tasks": [task("T1", [], "app/storage.py"), task("T2", ["T1"], "app/main.py")],
        "shared_artifacts": [],
    }


def output(task, content="VALUE = 1\n"):
    return {
        "task_id": task["task_id"],
        "files": [{"path": path, "content": content} for path in task["expected_artifacts"]],
        "implementation_summary": "Implemented the task for static review",
        "acceptance_check_mapping": [
            {"check": check, "implementation": "Covered in generated code"}
            for check in task["acceptance_checks"]
        ],
        "assumptions": [],
        "warnings": ["Runtime checks were not executed"],
        "completion_status": "completed",
    }


def response(payload):
    return SimpleNamespace(status="completed", output_parsed=payload)


def initial(payload, design_payload, developer_plan):
    plan = validate_execution_plan(
        ExecutionPlan.model_validate(developer_plan), RequirementsOutput.model_validate(payload)
    )
    return {
        "run_id": RUN_ID,
        "requirement": "Shorten URLs",
        "requirements": payload,
        "design": design_payload,
        "plan": plan.model_dump(),
        "status": "planning_complete",
        "through": "development",
        "development_budget": 20,
    }


def test_successful_task(isolated_llm, payload, design_payload, developer_plan):
    state = initial(payload, design_payload, developer_plan)
    state.update(prepare_development(state))
    state.update(select_task(state))
    assert state["plan"]["tasks"][0]["status"] == "in_progress"
    isolated_llm.responses.parse.return_value = response(output(developer_plan["tasks"][0]))
    state.update(developer_agent(state))
    assert state["plan"]["tasks"][0]["status"] == "completed"
    assert state["plan"]["ready_task_ids"] == ["T2"]
    assert (writer.OUTPUT_ROOT / RUN_ID / "app/storage.py").read_text() == "VALUE = 1\n"
    record = state["development_results"][0]
    assert record["attempt"] == 1 and record["status"] == "completed"
    assert record["runtime_checks_executed"] is False
    context = json.loads(isolated_llm.responses.parse.call_args.kwargs["input"][1]["content"])
    assert context["requirements"] == payload
    assert context["design"] == design_payload


@pytest.mark.parametrize(
    "problem", ["wrong_task", "unexpected_file", "syntax", "missing_mapping", "blocked", "invalid"]
)
def test_invalid_output(problem, isolated_llm, payload, design_payload, developer_plan):
    state = initial(payload, design_payload, developer_plan)
    state.update(prepare_development(state))
    state.update(select_task(state))
    data = output(developer_plan["tasks"][0])
    if problem == "wrong_task":
        data["task_id"] = "OTHER"
    elif problem == "unexpected_file":
        data["files"][0]["path"] = "workspace/output_code/other.py"
    elif problem == "syntax":
        data["files"][0]["content"] = "def invalid("
    elif problem == "missing_mapping":
        data["acceptance_check_mapping"] = []
    elif problem == "blocked":
        data["completion_status"] = "blocked"
    else:
        data = {}
    isolated_llm.responses.parse.return_value = response(data)
    state.update(developer_agent(state))
    assert state["plan"]["tasks"][0]["status"] == "failed"
    assert state["plan"]["tasks"][1]["status"] == "blocked"
    assert not (writer.OUTPUT_ROOT / RUN_ID / "app/storage.py").exists()


@pytest.mark.parametrize("problem", ["missing", "failed", "ambiguous", "cyclic", "invalid_design"])
def test_preconditions(problem, payload, design_payload, developer_plan):
    state = initial(payload, design_payload, developer_plan)
    if problem == "missing":
        state.pop("plan")
    elif problem == "failed":
        state["status"] = "failed"
    elif problem == "ambiguous":
        state["requirements"].update(requires_human_clarification=True, open_questions=["What?"])
    elif problem == "cyclic":
        state["plan"]["tasks"][0]["dependencies"] = ["T2"]
    else:
        state["design"] = {}
    assert prepare_development(state)["status"] == "failed"


@pytest.mark.parametrize(
    "path",
    [
        "/tmp/escape.py",
        "workspace/output_code/../escape.py",
        "workspace/output_code/.env",
        "workspace/output_code/credentials.json",
        "workspace/output_code/a;bad.py",
        "workspace/output_code/key.pem",
        "workspace/output_code/a\\b.py",
    ],
)
def test_unsafe_paths(path):
    change = DeveloperOutput.model_validate(
        {
            **output({"task_id": "T1", "expected_artifacts": [], "acceptance_checks": ["check"]}),
            "files": [{"path": path, "content": "pass"}],
        }
    )
    with writer.SafeWorkspace(RUN_ID) as workspace:
        with pytest.raises(ValueError):
            workspace.persist(change, "fingerprint")
    assert not list((writer.OUTPUT_ROOT / RUN_ID).glob("*.py"))


def test_symlink_escape(tmp_path):
    root = writer.OUTPUT_ROOT / RUN_ID
    root.mkdir(parents=True)
    external = tmp_path / "outside"
    external.mkdir()
    (root / "app").symlink_to(external, target_is_directory=True)
    with writer.SafeWorkspace(RUN_ID) as workspace:
        with pytest.raises(ValueError, match="Symlink"):
            workspace.persist(
                DeveloperOutput.model_validate(
                    output(
                        {
                            "task_id": "T1",
                            "expected_artifacts": ["workspace/output_code/app/main.py"],
                            "acceptance_checks": ["check"],
                        }
                    )
                ),
                "fp",
            )
    assert not list(external.iterdir())


def test_all_paths_validated_before_writes():
    data = output(
        {
            "task_id": "T1",
            "expected_artifacts": ["workspace/output_code/good.py", "workspace/output_code/.env"],
            "acceptance_checks": ["check"],
        }
    )
    with writer.SafeWorkspace(RUN_ID) as workspace:
        with pytest.raises(ValueError):
            workspace.persist(DeveloperOutput.model_validate(data), "fp")
    assert not (writer.OUTPUT_ROOT / RUN_ID / "good.py").exists()


def test_unrelated_file_not_overwritten():
    path = writer.OUTPUT_ROOT / RUN_ID / "app/main.py"
    path.parent.mkdir(parents=True)
    path.write_text("unrelated")
    data = DeveloperOutput.model_validate(
        output(
            {
                "task_id": "T1",
                "expected_artifacts": ["workspace/output_code/app/main.py"],
                "acceptance_checks": ["check"],
            }
        )
    )
    with writer.SafeWorkspace(RUN_ID) as workspace:
        with pytest.raises(ValueError, match="unrelated"):
            workspace.persist(data, "fp")
    assert path.read_text() == "unrelated"


def test_receipt_replay_and_tamper_detection():
    data = DeveloperOutput.model_validate(
        output(
            {
                "task_id": "T1",
                "expected_artifacts": ["workspace/output_code/app/main.py"],
                "acceptance_checks": ["check"],
            }
        )
    )
    with writer.SafeWorkspace(RUN_ID) as workspace:
        workspace.persist(data, "fp")
    with writer.SafeWorkspace(RUN_ID) as workspace:
        assert workspace.receipt("T1", "fp") == data
    (writer.OUTPUT_ROOT / RUN_ID / "app/main.py").write_text("manual edit")
    with writer.SafeWorkspace(RUN_ID) as workspace:
        with pytest.raises(ValueError, match="modified"):
            workspace.snapshot()


def test_write_failure_recovery(monkeypatch):
    data = DeveloperOutput.model_validate(
        output(
            {
                "task_id": "T1",
                "expected_artifacts": ["workspace/output_code/a.py", "workspace/output_code/b.py"],
                "acceptance_checks": ["check"],
            }
        )
    )
    real_write = writer.atomic_write

    def fail_second(path, content):
        if path.name == "b.py":
            raise OSError("disk failure")
        real_write(path, content)

    with writer.SafeWorkspace(RUN_ID) as workspace:
        monkeypatch.setattr(writer, "atomic_write", fail_second)
        with pytest.raises(OSError):
            workspace.persist(data, "fp")
    monkeypatch.setattr(writer, "atomic_write", real_write)
    with writer.SafeWorkspace(RUN_ID) as workspace:
        assert workspace.receipt("T1", "fp") == data
    assert (writer.OUTPUT_ROOT / RUN_ID / "b.py").read_text() == "VALUE = 1\n"


def test_partial_failure_resume(
    tmp_path, capsys, isolated_llm, payload, design_payload, developer_plan
):
    isolated_llm.responses.parse.side_effect = [
        response(payload),
        response(design_payload),
        response(developer_plan),
        response(output(developer_plan["tasks"][0])),
        response(None),
    ]
    assert (
        run.main(
            [
                "--json",
                "start",
                "--stop-after",
                "development",
                "--requirement",
                "Build a URL shortener",
            ]
        )
        == 0
    )
    paused = json.loads(capsys.readouterr().out)
    assert paused["status"] == "paused"
    assert paused["plan"]["tasks"][0]["status"] == "completed"
    assert paused["plan"]["tasks"][1]["status"] == "failed"
    isolated_llm.responses.parse.side_effect = [response(output(developer_plan["tasks"][1]))]
    assert run.main(["--json", "resume", "--run-id", paused["run_id"], "--retry-development"]) == 0
    done = json.loads(capsys.readouterr().out)
    assert done["status"] == "development_complete"
    assert [r["task_id"] for r in done["development_results"]] == ["T1", "T2", "T2"]
    assert [r["status"] for r in done["development_results"]] == [
        "completed",
        "failed",
        "completed",
    ]
    assert done["thread_id"] == paused["thread_id"]
    assert (run.RUNS_DIR / done["run_id"] / "development_results.json").is_file()


def test_budget_resume(capsys, isolated_llm, payload, design_payload, developer_plan):
    isolated_llm.responses.parse.side_effect = [
        response(payload),
        response(design_payload),
        response(developer_plan),
        response(output(developer_plan["tasks"][0])),
    ]
    assert (
        run.main(
            [
                "--json",
                "start",
                "--through",
                "development",
                "--development-budget",
                "1",
                "--requirement",
                "Build a URL shortener",
            ]
        )
        == 0
    )
    paused = json.loads(capsys.readouterr().out)
    assert paused["status"] == "paused"
    assert len(paused["development_results"]) == 1
    isolated_llm.responses.parse.side_effect = [response(output(developer_plan["tasks"][1]))]
    assert run.main(["--json", "resume", "--run-id", paused["run_id"], "--retry-development"]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "development_complete"


def test_llm_error(isolated_llm, payload, design_payload, developer_plan):
    import httpx
    from openai import APIConnectionError

    state = initial(payload, design_payload, developer_plan)
    state.update(prepare_development(state))
    state.update(select_task(state))
    isolated_llm.responses.parse.side_effect = APIConnectionError(
        request=httpx.Request("POST", "https://api.openai.com")
    )
    result = developer_agent(state)
    assert result["development_results"][0]["status"] == "failed"
    assert result["plan"]["tasks"][1]["status"] == "blocked"


def test_missing_dependency_never_generated(isolated_llm, payload, design_payload, developer_plan):
    state = initial(payload, design_payload, developer_plan)
    state.update(prepare_development(state))
    state.update(select_task(state))
    state["plan"]["tasks"][0]["dependencies"] = ["MISSING"]
    assert developer_agent(state)["development_results"][0]["status"] == "failed"
    isolated_llm.responses.parse.assert_not_called()


def test_checkpoint_replay_uses_receipt(
    monkeypatch, capsys, isolated_llm, payload, design_payload, developer_plan
):
    real_persist = writer.SafeWorkspace.persist
    interrupted = False

    def crash_after_write(self, output, fingerprint):
        nonlocal interrupted
        result = real_persist(self, output, fingerprint)
        if not interrupted:
            interrupted = True
            raise KeyboardInterrupt("simulate process exit after file persistence")
        return result

    monkeypatch.setattr(writer.SafeWorkspace, "persist", crash_after_write)
    isolated_llm.responses.parse.side_effect = [
        response(payload),
        response(design_payload),
        response(developer_plan),
        response(output(developer_plan["tasks"][0])),
        response(output(developer_plan["tasks"][1])),
    ]
    with pytest.raises(KeyboardInterrupt):
        run.main(
            [
                "--json",
                "start",
                "--through",
                "development",
                "--requirement",
                "Build a URL shortener",
            ]
        )
    capsys.readouterr()
    run_id = next(run.RUNS_DIR.iterdir()).name
    assert run.main(["--json", "resume", "--run-id", run_id, "--retry-development"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["status"] == "development_complete"
    assert isolated_llm.responses.parse.call_count == 5  # Receipt avoided regenerating T1.


def test_retry_limit(capsys, isolated_llm, payload, design_payload, developer_plan):
    isolated_llm.responses.parse.side_effect = [
        response(payload),
        response(design_payload),
        response(developer_plan),
        response(None),
    ]
    assert (
        run.main(
            [
                "--json",
                "start",
                "--through",
                "development",
                "--requirement",
                "Build a URL shortener",
            ]
        )
        == 0
    )
    report = json.loads(capsys.readouterr().out)
    for _ in range(2):
        isolated_llm.responses.parse.side_effect = [response(None)]
        assert (
            run.main(["--json", "resume", "--run-id", report["run_id"], "--retry-development"]) == 0
        )
        report = json.loads(capsys.readouterr().out)
    calls = isolated_llm.responses.parse.call_count
    assert run.main(["--json", "resume", "--run-id", report["run_id"], "--retry-development"]) == 1
    report = json.loads(capsys.readouterr().out)
    assert report["status"] == "failed"
    assert isolated_llm.responses.parse.call_count == calls


def test_development_rejection(capsys, isolated_llm, payload, design_payload, developer_plan):
    isolated_llm.responses.parse.side_effect = [
        response(payload),
        response(design_payload),
        response(developer_plan),
        response(None),
    ]
    assert (
        run.main(
            [
                "--json",
                "start",
                "--through",
                "development",
                "--requirement",
                "Build a URL shortener",
            ]
        )
        == 0
    )
    paused = json.loads(capsys.readouterr().out)
    assert run.main(["--json", "resume", "--run-id", paused["run_id"], "--reject"]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "rejected"


def test_duplicate_paths_rejected():
    data = output(
        {
            "task_id": "T1",
            "expected_artifacts": ["workspace/output_code/a.py"] * 2,
            "acceptance_checks": ["check"],
        }
    )
    with writer.SafeWorkspace(RUN_ID) as workspace:
        with pytest.raises(ValueError, match="Duplicate"):
            workspace.persist(DeveloperOutput.model_validate(data), "fp")
    assert not (writer.OUTPUT_ROOT / RUN_ID / "a.py").exists()


def test_file_symlink_rejected(tmp_path):
    external = tmp_path / "outside.py"
    external.write_text("outside")
    root = writer.OUTPUT_ROOT / RUN_ID
    root.mkdir(parents=True)
    (root / "a.py").symlink_to(external)
    data = output(
        {
            "task_id": "T1",
            "expected_artifacts": ["workspace/output_code/a.py"],
            "acceptance_checks": ["check"],
        }
    )
    with writer.SafeWorkspace(RUN_ID) as workspace:
        with pytest.raises(ValueError, match="Symlink"):
            workspace.persist(DeveloperOutput.model_validate(data), "fp")
    assert external.read_text() == "outside"


def test_harness_pytest_scope():
    import configparser
    from pathlib import Path

    config = configparser.ConfigParser()
    config.read(Path(__file__).resolve().parents[1] / "pytest.ini")
    assert config["pytest"]["testpaths"].split() == ["tests"]


def test_zero_budget_rejected(payload, design_payload, developer_plan):
    state = initial(payload, design_payload, developer_plan)
    state["development_budget"] = 0
    assert prepare_development(state)["status"] == "failed"


def test_size_limit(monkeypatch):
    monkeypatch.setattr(writer, "MAX_FILE_BYTES", 2)
    data = output(
        {
            "task_id": "T1",
            "expected_artifacts": ["workspace/output_code/a.py"],
            "acceptance_checks": ["check"],
        }
    )
    with writer.SafeWorkspace(RUN_ID) as workspace:
        with pytest.raises(ValueError, match="size budget"):
            workspace.persist(DeveloperOutput.model_validate(data), "fp")
    assert not (writer.OUTPUT_ROOT / RUN_ID / "a.py").exists()


def test_parent_file_conflict_checked_before_writes():
    data = output(
        {
            "task_id": "T1",
            "expected_artifacts": ["workspace/output_code/a.py", "workspace/output_code/a.py/b.py"],
            "acceptance_checks": ["check"],
        }
    )
    with writer.SafeWorkspace(RUN_ID) as workspace:
        with pytest.raises(ValueError, match="conflict"):
            workspace.persist(DeveloperOutput.model_validate(data), "fp")
    assert not (writer.OUTPUT_ROOT / RUN_ID / "a.py").exists()
