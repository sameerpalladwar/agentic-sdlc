import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
import agents.qa as qa
import orchestrator.qa_runner as runner
from orchestrator import file_writer
from orchestrator.file_writer import SafeWorkspace
from models.qa import QAResult

@pytest.fixture
def app_directory(tmp_path, monkeypatch):
    monkeypatch.setattr(file_writer, "OUTPUT_ROOT", tmp_path / "output")
    path = file_writer.OUTPUT_ROOT / ("a" * 32)
    path.mkdir(parents=True)
    monkeypatch.setattr(runner, "qa_python", lambda run_id: Path(sys.executable))
    return path

@pytest.mark.parametrize("code, output, status, counts", [
    (0, "2 passed, 1 skipped in 0.10s", "qa_passed", (3, 2, 0, 1)),
    (1, "1 failed, 1 passed in 0.10s", "qa_failed", (2, 1, 1, 0)),
    (5, "no tests ran in 0.01s", "qa_blocked", (0, 0, 0, 0)),
    (0, "", "qa_blocked", (0, 0, 0, 0)),
    (0, "1 skipped in 0.01s", "qa_blocked", (1, 0, 0, 1)),
    (2, "ModuleNotFoundError: dependency", "qa_failed", (0, 0, 0, 0)),
])
def test_pytest_results(app_directory, monkeypatch, code, output, status, counts):
    def execute(command, **kwargs):
        assert command == [sys.executable, "-m", "pytest", "-q"]
        assert kwargs["shell"] is False and kwargs["timeout"] == 60
        assert kwargs["cwd"] != app_directory
        assert "OPENAI_API_KEY" not in kwargs["env"]
        kwargs["stdout"].write(output.encode())
        return SimpleNamespace(returncode=code)
    monkeypatch.setattr(runner.subprocess, "run", execute)
    result = runner.run_tests("a" * 32, {}, local_authorized=True)
    assert result.status == status
    assert (result.total_tests, result.passed_tests, result.failed_tests, result.skipped_tests) == counts
    assert result.exit_code == code

@pytest.mark.parametrize("error, status", [(subprocess.TimeoutExpired("pytest", 60), "qa_failed"), (OSError("unavailable"), "qa_blocked")])
def test_execution_errors(app_directory, monkeypatch, error, status):
    def execute(*args, **kwargs): raise error
    monkeypatch.setattr(runner.subprocess, "run", execute)
    result = runner.run_tests("a" * 32, {}, local_authorized=True)
    assert result.status == status and result.total_tests == 0

def test_output_limit(app_directory, monkeypatch):
    def execute(*args, **kwargs):
        kwargs["stdout"].write(b"x" * (runner.OUTPUT_LIMIT + 1))
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(runner.subprocess, "run", execute)
    result = runner.run_tests("a" * 32, {}, local_authorized=True)
    assert result.status == "qa_failed" and len(result.test_output) == runner.OUTPUT_LIMIT

def test_requires_approval(app_directory, monkeypatch):
    monkeypatch.setattr(runner.subprocess, "run", lambda *a, **k: pytest.fail("Unapproved execution"))
    assert runner.run_tests("a" * 32, {}).status == "qa_blocked"

@pytest.mark.parametrize("path", ["/tmp", "../escape", "/missing"])
def test_unsafe_or_missing_directory(app_directory, path):
    assert runner.run_tests("a" * 32, {}, application_dir=path).status == "qa_blocked"

def test_missing_directory(app_directory):
    app_directory.rmdir()
    assert runner.run_tests("a" * 32, {}).status == "qa_blocked"

def test_symlink_directory(app_directory, tmp_path):
    app_directory.rmdir()
    app_directory.symlink_to(tmp_path)
    assert runner.run_tests("a" * 32, {}).status == "qa_blocked"

def test_secret_filtering(tmp_path, monkeypatch):
    for name in ["OPENAI_API_KEY", "AWS_SECRET_ACCESS_KEY", "DATABASE_PASSWORD", "PYTEST_ADDOPTS"]:
        monkeypatch.setenv(name, "sensitive")
    environment = runner.clean_environment(tmp_path)
    assert "sensitive" not in environment.values()
    assert "PYTEST_ADDOPTS" not in environment

def test_unsafe_command():
    with pytest.raises(ValueError, match="approved"):
        runner.run_tests("a" * 32, {}, ("sh", "-c", "pytest"))

def completed_state(payload, design_payload, run_id):
    from models.developer import DeveloperOutput
    from models.planning import ExecutionPlan
    task = {"task_id": "T1", "title": "Implement", "description": "Implement lookup",
            "related_user_story_ids": ["US-001"], "dependencies": [],
            "expected_artifacts": ["workspace/output_code/test_app.py"],
            "acceptance_checks": ["Lookup returns original URL"], "status": "completed"}
    output = DeveloperOutput.model_validate({"task_id": "T1", "files": [{"path": task["expected_artifacts"][0], "content": "def test_it():\n    assert True\n"}],
            "implementation_summary": "Trusted mock source", "acceptance_check_mapping": [{"check": task["acceptance_checks"][0], "implementation": "mock"}],
            "assumptions": [], "warnings": [], "completion_status": "completed"})
    with SafeWorkspace(run_id) as workspace:
        workspace.persist(output, "fixture")
    return {"requirement": "Shorten URLs", "run_id": run_id, "status": "development_complete", "requirements": payload,
            "design": design_payload, "plan": ExecutionPlan(tasks=[task], shared_artifacts=[]).model_dump(),
            "development_results": [{"task_id": "T1", "status": "completed", "result": output.model_dump()}]}



@pytest.mark.parametrize("status", ["qa_passed", "qa_failed", "qa_blocked"])
def test_node_results(app_directory, monkeypatch, payload, design_payload, status):
    state = completed_state(payload, design_payload, "a" * 32)
    monkeypatch.setattr(qa, "run_tests", lambda *a, **k: QAResult(status=status))
    result = qa.qa_agent(state)
    assert result["status"] == status
    assert result["qa_results"]["acceptance_criteria_results"][0]["status"] == "not_verified"

def test_invalid_node_input():
    assert qa.qa_agent({"status": "failed"})["status"] == "qa_blocked"

@pytest.mark.parametrize("name, status", [("passing", "qa_passed"), ("failing", "qa_failed")])
def test_trusted_fixture(app_directory, name, status):
    fixtures = Path(__file__).parent / "fixtures" / "qa"
    files = {"workspace/output_code/application.py": (fixtures / "application.py").read_text(),
             "workspace/output_code/test_app.py": (fixtures / name / "checks.py").read_text()}
    result = runner.run_tests("a" * 32, files, local_authorized=True)
    assert result.status == status and result.total_tests == 1

@pytest.mark.parametrize("success", [True, False])
def test_workflow_and_persistence(tmp_path, monkeypatch, capsys, isolated_llm, payload, design_payload, success):
    import copy
    import run
    from tests.conftest import approved_review
    monkeypatch.setattr("agents.reviewer.generate_review", approved_review)
    monkeypatch.setattr(file_writer, "OUTPUT_ROOT", tmp_path / "output")
    monkeypatch.setattr(run, "RUNS_DIR", tmp_path / "runs")
    fixture = completed_state(payload, design_payload, "a" * 32)
    plan = copy.deepcopy(fixture["plan"])
    plan["tasks"][0]["status"] = "ready"
    isolated_llm.responses.parse.side_effect = [SimpleNamespace(status="completed", output_parsed=r) for r in
        [payload, design_payload, plan, fixture["development_results"][0]["result"]]]
    # Start always blocks without explicit approval, then resume the same checkpoint.
    assert run.main(["start", "--stop-after", "qa", "--requirement", "Shorten URLs"]) == 3
    report = json.loads(capsys.readouterr().out)
    run_id = report["run_id"]
    assert report["status"] == "validation_blocked"
    def execute(*args, **kwargs):
        assert kwargs["local_authorized"] is True
        return QAResult(status="qa_passed" if success else "qa_failed", exit_code=0 if success else 1,
                        total_tests=1, passed_tests=int(success), failed_tests=int(not success))
    monkeypatch.setattr(qa, "run_tests", execute)
    assert run.main(["qa", "--run-id", run_id, "--authorize-local-execution"]) == (0 if success else 1)
    report = json.loads(capsys.readouterr().out)
    assert report["run_id"] == run_id
    assert report["status"] == ("validation_passed" if success else "validation_failed")
    assert json.loads((run.RUNS_DIR / run_id / "qa_results.json").read_text()) == report["qa_results"]
    assert isolated_llm.responses.parse.call_count == 4

    assert json.loads((run.RUNS_DIR / run_id / "review_results.json").read_text()) == report["review_result"]
    assert json.loads((run.RUNS_DIR / run_id / "validation_results.json").read_text()) == report["validation_result"]
    assert report["qa_result"]["revision"] == report["review_result"]["reviewed_revision"]
