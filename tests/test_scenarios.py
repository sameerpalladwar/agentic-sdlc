"""Automatic routing uses deterministic input and durable human gates, never a classifier LLM."""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import run
from agents.repository_analysis import inspect_repository
from orchestrator import file_writer
from orchestrator.scenarios import infer_mode


@pytest.fixture(autouse=True)
def scenario_workspace(tmp_path, monkeypatch):
    monkeypatch.setattr(run, "RUNS_DIR", tmp_path / "runs")
    monkeypatch.setattr(file_writer, "OUTPUT_ROOT", tmp_path / "output_code")
    monkeypatch.setattr("builtins.input", lambda: pytest.fail("Routing must not block on stdin"))


@pytest.fixture
def repository(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    (root / "application.py").write_text("VALUE=1\n")
    return root


def cli(capsys, *args):
    partial = ["--stop-after", "requirements"] if "--requirement" in args else []
    assert run.main([*args, *partial, "--json", "--no-progress"]) == 0
    return json.loads(capsys.readouterr().out)


@pytest.mark.parametrize(
    "text,mode",
    [
        ("Build a URL shortener", "greenfield"),
        ("Build a URL management system for our team", "greenfield"),
        ("Add URL expiration to my existing URL shortener", "brownfield"),
        ("Fix redirect errors", "brownfield"),
        ("Enhance the application", "brownfield"),
        ("URL links for a team", None),
        ("Something useful", None),
    ],
)
def test_inference(text, mode):
    assert infer_mode(text) == mode


def test_greenfield_and_legacy_cli(capsys, llm_response):
    for prefix in (["start"], []):
        result = cli(capsys, *prefix, "--requirement", "Build a URL shortener")
        assert result["status"] == "requirements_complete"
        assert result["project_mode"] == "greenfield"
        assert not result["repository_analysis"]
    assert llm_response.call_count == 2  # No classification API calls.


def test_brownfield_repo_overrides_creation_intent(capsys, repository, llm_response):
    result = cli(
        capsys, "start", "--repo-path", str(repository), "--requirement", "Build a feature"
    )
    assert result["project_mode"] == "brownfield"
    assert result["repository_analysis"]["python_modules"][0]["path"] == "application.py"
    assert llm_response.call_count == 1
    assert "repository_analysis" in llm_response.call_args.kwargs["input"][2]["content"]


def test_missing_repository_restart_resume_same_run(capsys, repository, isolated_llm, llm_response):
    requirement = "Add URL expiration to my existing URL shortener"
    result = cli(capsys, "start", "--requirement", requirement)
    assert result["status"] == "paused" and result["pause"]["kind"] == "repository"
    assert result["project_mode"] == "brownfield"
    llm_response.assert_not_called()
    directory = run.RUNS_DIR / result["run_id"]
    saved = json.loads((directory / "status.json").read_text())
    assert saved["requirement"] == requirement and saved["project_questions"]
    resumed = cli(capsys, "resume", "--run-id", result["run_id"], "--repo-path", str(repository))
    assert resumed["status"] == "requirements_complete"
    assert resumed["run_id"] == result["run_id"] == resumed["thread_id"]
    assert resumed["requirement"] == requirement and len(resumed["project_input_history"]) == 1
    assert len(list(run.RUNS_DIR.iterdir())) == 1
    assert (repository / "application.py").read_text() == "VALUE=1\n"
    assert llm_response.call_count == 1


def test_unclear_intent_and_repeated_unclear_answer(capsys, llm_response):
    result = cli(capsys, "start", "--requirement", "URL links for a team")
    assert result["pause"]["kind"] == "project_intent" and result["project_mode"] is None
    llm_response.assert_not_called()
    again = cli(capsys, "resume", "--run-id", result["run_id"], "--answers", "unsure")
    assert again["status"] == "paused"
    done = cli(
        capsys, "resume", "--run-id", result["run_id"], "--answers", "Build a new application"
    )
    assert done["status"] == "requirements_complete" and done["project_mode"] == "greenfield"
    assert done["requirement"] == "URL links for a team"


@pytest.mark.parametrize("kind", ["missing", "file", "empty", "docs", "unreadable", "symlink"])
def test_invalid_repo_correctable(kind, tmp_path, repository, capsys, llm_response):
    path = tmp_path / "invalid"
    if kind == "file":
        path.write_text("not a directory")
    elif kind == "symlink":
        path.symlink_to(repository, target_is_directory=True)
    elif kind != "missing":
        path.mkdir()
        if kind == "docs":
            (path / "README.md").write_text("Documentation only")
        if kind == "unreadable":
            path.chmod(0)
    try:
        result = cli(capsys, "start", "--requirement", "Add expiration", "--repo-path", str(path))
        assert result["status"] == "paused" and result["error"]
        assert result["pause"]["kind"] == "repository"
        llm_response.assert_not_called()
        again = cli(capsys, "resume", "--run-id", result["run_id"], "--repo-path", str(path))
        assert again["status"] == "paused"
        done = cli(capsys, "resume", "--run-id", result["run_id"], "--repo-path", str(repository))
        assert done["status"] == "requirements_complete" and done["error"] is None
        assert done["repository_path"] == str(repository)
        assert llm_response.call_count == 1
    finally:
        if kind == "unreadable":
            path.chmod(0o755)


@pytest.mark.parametrize("requirement", ["Add expiration", "URL management"])
def test_cancel(capsys, llm_response, requirement):
    result = cli(capsys, "start", "--requirement", requirement)
    rejected = cli(capsys, "resume", "--run-id", result["run_id"], "--reject")
    assert rejected["status"] == "rejected"
    llm_response.assert_not_called()
    assert not file_writer.OUTPUT_ROOT.exists()


@pytest.mark.parametrize("tty", [False, True])
def test_pause_message_never_reads_stdin(capsys, monkeypatch, tty, isolated_llm):
    monkeypatch.setattr(run.sys.stdin, "isatty", lambda: tty)
    assert run.main(["start", "--requirement", "Fix existing application", "--no-progress"]) == 0
    text = capsys.readouterr().out
    assert "paused" in text and "Question:" in text
    assert "python run.py resume --run-id" in text and "--repo-path" in text
    isolated_llm.responses.parse.assert_not_called()


@pytest.mark.parametrize("brownfield", [False, True])
def test_requirements_ambiguity_both_modes(capsys, llm_response, payload, repository, brownfield):
    payload.update(
        requires_human_clarification=True, open_questions=["Which behavior is required?"]
    )
    args = ["--repo-path", str(repository)] if brownfield else []
    result = cli(
        capsys, "start", "--requirement", "Build a URL management system for our team", *args
    )
    assert result["status"] == "paused" and result["open_questions"] == [
        "Which behavior is required?"
    ]
    payload.update(requires_human_clarification=False, open_questions=[])
    done = cli(capsys, "resume", "--run-id", result["run_id"], "--answers", "Shorten URLs")
    assert done["status"] == "requirements_complete"
    assert done["repository_analysis"] == result["repository_analysis"]
    events = [
        json.loads(line)
        for line in (run.RUNS_DIR / done["run_id"] / "events.jsonl").read_text().splitlines()
    ]
    assert sum(
        e["event"] == "agent_started" and e["stage"] == "repository_analysis" for e in events
    ) == int(brownfield)
    assert llm_response.call_count == 2


def test_scenario_docs_not_runtime_inputs(capsys, llm_response, monkeypatch):
    # Runtime never opens documentation; requirements remain the only primary input.
    original = Path.read_text

    def guarded(path, *args, **kwargs):
        assert "scenarios" not in path.parts
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", guarded)
    assert (
        cli(capsys, "start", "--requirement", "Build a URL shortener")["project_mode"]
        == "greenfield"
    )


def test_no_menu_without_arguments():
    with pytest.raises(SystemExit) as error:
        run.main([])
    assert error.value.code == 2


def test_repository_analysis_excludes_sensitive_and_external_files(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    (root / "application.py").write_text("VALUE=1\n")
    (root / ".env").write_text("OPENAI_API_KEY=do-not-read")
    (root / "credentials.json").write_text("do-not-read")
    (root / "cache.db").write_bytes(b"not source")
    (root / "image.py").write_bytes(b"\0binary")
    (root / ".venv").mkdir()
    (root / ".venv" / "inside.py").write_text("ignore")
    outside = tmp_path / "outside.py"
    outside.write_text("do-not-read")
    (root / "link.py").symlink_to(outside)
    analysis, files = inspect_repository(str(root))
    assert files == {"workspace/output_code/application.py": "VALUE=1\n"}
    assert analysis["excluded_files"] >= 5
    assert inspect_repository(str(root))[0]["revision"] == analysis["revision"]


@pytest.mark.parametrize("interrupted", [False, True])
def test_brownfield_developer_sees_managed_baseline(
    tmp_path, monkeypatch, capsys, isolated_llm, payload, design_payload, interrupted
):
    source = tmp_path / "original"
    source.mkdir()
    (source / "application.py").write_text("VALUE = 1\n")
    (source / "README.md").write_text("Python 3.12 existing application\n")
    task = {
        "task_id": "T1",
        "title": "Enhance",
        "description": "Enhance existing code",
        "related_user_story_ids": ["US-001"],
        "dependencies": [],
        "expected_artifacts": ["workspace/output_code/application.py"],
        "acceptance_checks": ["Preserve existing code and enhance behavior"],
        "status": "pending",
    }
    developer = {
        "task_id": "T1",
        "files": [{"path": task["expected_artifacts"][0], "content": "VALUE = 2\n"}],
        "implementation_summary": "Mock targeted enhancement",
        "acceptance_check_mapping": [
            {"check": task["acceptance_checks"][0], "implementation": "Update constant"}
        ],
        "assumptions": [],
        "warnings": ["Tests not executed"],
        "completion_status": "completed",
    }
    isolated_llm.responses.parse.side_effect = [
        SimpleNamespace(status="completed", output_parsed=value)
        for value in [payload, design_payload, {"tasks": [task], "shared_artifacts": []}, developer]
    ]
    arguments = [
        "start",
        "--requirement",
        "Enhance existing application",
        "--repo-path",
        str(source),
        "--stop-after",
        "development",
        "--json",
    ]
    if interrupted:
        import orchestrator.graph as workflow

        original = workflow.prepare_development

        def stop_after_copy(state):
            original(state)
            raise RuntimeError("Simulated process interruption after baseline copy")

        monkeypatch.setattr(workflow, "prepare_development", stop_after_copy)
        with pytest.raises(RuntimeError):
            run.main(arguments)
        run_id = next(run.RUNS_DIR.iterdir()).name
        monkeypatch.setattr(workflow, "prepare_development", original)
        arguments = ["resume", "--run-id", run_id, "--retry-development", "--json"]
    assert run.main(arguments) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["status"] == "development_complete"
    assert (source / "application.py").read_text() == "VALUE = 1\n"
    output = file_writer.OUTPUT_ROOT / report["run_id"]
    assert (output / "application.py").read_text() == "VALUE = 2\n"
    assert (output / "README.md").read_text() == (source / "README.md").read_text()
    context = json.loads(isolated_llm.responses.parse.call_args.kwargs["input"][1]["content"])
    assert context["project_mode"] == "brownfield"
    assert context["existing_files"]["workspace/output_code/application.py"] == "VALUE = 1\n"


def test_scenario_preserves_execution_authorization_and_human_approval(
    capsys, monkeypatch, isolated_llm, payload, design_payload
):
    from models.qa import QAResult
    from tests.conftest import approved_review
    from tests.test_qa import completed_state

    # The selected definition explicitly requires both endpoints; mock design must honor them.
    design_payload["api_endpoints"][0]["path"] = "/shorten"
    design_payload["api_endpoints"].append(
        {
            "method": "GET",
            "path": "/{short_code}",
            "request": "Short code",
            "response": "307 redirect",
            "errors": ["404 unknown code"],
        }
    )
    design_payload["story_traceability"][0]["design_elements"] = ["URL API POST /shorten"]
    fixture = completed_state(payload, design_payload, "a" * 32)
    fixture["plan"]["tasks"][0]["status"] = "pending"
    responses = [
        payload,
        design_payload,
        fixture["plan"],
        fixture["development_results"][0]["result"],
    ]
    isolated_llm.responses.parse.side_effect = [
        SimpleNamespace(status="completed", output_parsed=value) for value in responses
    ]
    monkeypatch.setattr("agents.reviewer.generate_review", approved_review)
    monkeypatch.setattr(
        "builtins.input", lambda: pytest.fail("Direct scenario commands must not prompt")
    )
    assert (
        run.main(
            [
                "start",
                "--requirement",
                "Build a URL shortener",
                "--json",
            ]
        )
        == 3
    )
    blocked = json.loads(capsys.readouterr().out)
    assert blocked["status"] == "validation_blocked"
    assert blocked["qa_result"]["status"] == "qa_blocked"
    assert blocked["through"] == "delivery" and not blocked["human_approval"]
    monkeypatch.setattr(
        "agents.qa.run_tests",
        lambda *a, **k: QAResult(status="qa_passed", exit_code=0, total_tests=1, passed_tests=1),
    )
    assert (
        run.main(["qa", "--run-id", blocked["run_id"], "--authorize-local-execution", "--json"])
        == 0
    )
    paused = json.loads(capsys.readouterr().out)
    assert paused["status"] == "paused" and paused["pause"]["kind"] == "approval"
    assert paused["run_id"] == blocked["run_id"] and paused["project_mode"] == "greenfield"
    assert not paused["human_approval"] and not paused["final_manifest"]


def test_project_pause_resume_across_processes(tmp_path, repository):
    """New CLI processes reopen the same SQLite thread; no API or source execution."""
    import subprocess
    import sys

    script = """
import sys
from pathlib import Path
import run
import orchestrator.graph as workflow

def offline_requirements(state):
    return {"status": "requirements_complete", "requirements": {"summary": "Offline"}, "error": None}

workflow.requirements_agent = offline_requirements
run.RUNS_DIR = Path(sys.argv[1])
raise SystemExit(run.main([*sys.argv[2:], "--json", "--no-progress", *(["--stop-after", "requirements"] if "start" in sys.argv[2:] else [])]))
"""

    def process(*args):
        result = subprocess.run(
            [sys.executable, "-c", script, str(tmp_path / "runs"), *args],
            capture_output=True,
            text=True,
            check=True,
        )
        return json.loads(result.stdout)

    pending = process("start", "--requirement", "Fix existing application")
    assert pending["status"] == "paused"
    assert pending["run_id"] in pending["pause"]["resume_command"]
    done = process("resume", "--run-id", pending["run_id"], "--repo-path", str(repository))
    assert done["run_id"] == pending["run_id"] and done["status"] == "requirements_complete"
    assert (
        done["requirement"] == "Fix existing application" and done["project_mode"] == "brownfield"
    )


def test_unclear_intent_can_resume_to_brownfield_then_request_repo(
    capsys, llm_response, repository
):
    pending = cli(capsys, "start", "--requirement", "URL management")
    brownfield = cli(
        capsys, "resume", "--run-id", pending["run_id"], "--answers", "Modify existing application"
    )
    assert (
        brownfield["project_mode"] == "brownfield" and brownfield["pause"]["kind"] == "repository"
    )
    llm_response.assert_not_called()
    done = cli(capsys, "resume", "--run-id", pending["run_id"], "--repo-path", str(repository))
    assert done["status"] == "requirements_complete" and done["run_id"] == pending["run_id"]


def test_no_changes_to_scenario_documentation_inventory():
    directory = Path(__file__).resolve().parents[1] / "scenarios"
    assert sorted(path.name for path in directory.iterdir()) == [
        "ambiguous.md",
        "brownfield.md",
        "greenfield.md",
    ]
