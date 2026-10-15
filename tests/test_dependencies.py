import pytest

from orchestrator.dependencies import FASTAPI_PINS, validate_dependencies


def application():
    return {
        "workspace/output_code/requirements.txt": "\n".join(
            f"{n}=={v}" for n, v in FASTAPI_PINS.items()
        ),
        "workspace/output_code/README.md": "Target: Python 3.12",
        "workspace/output_code/app/main.py": "import fastapi\nimport sqlite3\nfrom app import storage\n",
        "workspace/output_code/app/storage.py": "import sqlite3\n",
    }


def test_supported_profile():
    assert validate_dependencies(application(), final=True) == []


@pytest.mark.parametrize(
    "line,code",
    [
        ("sqlite3", "stdlib_dependency"),
        ("json==1.0", "stdlib_dependency"),
        ("httpx>=0.24,<0.25", "invalid_dependency"),
        ("httpx==0.24.1", "incompatible_dependency"),
        ("-r other.txt", "invalid_dependency"),
        ("httpx @ https://example.com/x", "invalid_dependency"),
    ],
)
def test_invalid_dependencies(line, code):
    files = application()
    files["workspace/output_code/requirements.txt"] = (
        files["workspace/output_code/requirements.txt"].replace("httpx==0.28.1", "") + "\n" + line
    )
    assert code in {i["code"] for i in validate_dependencies(files, final=True)}


def test_missing_import():
    files = application()
    files["workspace/output_code/app/storage.py"] = "import sqlalchemy\n"
    assert any("sqlalchemy" in i["message"] for i in validate_dependencies(files, final=True))


@pytest.mark.parametrize("version", ["3.14", "3.13", "3.11"])
def test_unsupported_runtime(version):
    files = application()
    files["workspace/output_code/README.md"] = "Python " + version
    assert "unsupported_python" in {i["code"] for i in validate_dependencies(files, final=True)}


def test_missing_requirements():
    files = application()
    del files["workspace/output_code/requirements.txt"]
    assert "missing_dependency" in {i["code"] for i in validate_dependencies(files, final=True)}


def test_development_cannot_complete_with_invalid_dependencies(
    tmp_path, monkeypatch, payload, design_payload
):
    from orchestrator import file_writer
    from orchestrator.development import select_task
    from tests.test_qa import completed_state

    monkeypatch.setattr(file_writer, "OUTPUT_ROOT", tmp_path / "output")
    state = completed_state(payload, design_payload, "a" * 32)
    monkeypatch.setattr(
        "orchestrator.development.validate_dependencies",
        lambda *a, **k: [
            {"code": "missing_dependency", "field": "requirements.txt", "message": "missing httpx"}
        ],
    )
    result = select_task(state)
    assert result["status"] == "failed"
    assert result["development_issues"][0]["code"] == "missing_dependency"


def test_invalid_task_not_persisted(tmp_path, monkeypatch, payload, design_payload):
    from models.developer import DeveloperOutput
    from orchestrator import file_writer
    from orchestrator.development import developer_agent, prepare_development, select_task
    from tests.test_developer import initial, output

    monkeypatch.setattr(file_writer, "OUTPUT_ROOT", tmp_path / "output")
    plan = {
        "tasks": [
            {
                "task_id": "T1",
                "title": "Dependencies",
                "description": "Configure packages",
                "related_user_story_ids": ["US-001"],
                "dependencies": [],
                "expected_artifacts": ["workspace/output_code/requirements.txt"],
                "acceptance_checks": ["Consistent dependencies"],
                "status": "pending",
            }
        ],
        "shared_artifacts": [],
    }
    state = initial(payload, design_payload, plan)
    state.update(prepare_development(state))
    state.update(select_task(state))
    bad = DeveloperOutput.model_validate(output(state["plan"]["tasks"][0], "sqlite3\n"))
    monkeypatch.setattr("orchestrator.development.generate_task", lambda context: bad)
    result = developer_agent(state)
    assert result["plan"]["tasks"][0]["status"] == "failed"
    assert result["development_results"][0]["issues"][0]["code"] == "stdlib_dependency"
    assert not (file_writer.OUTPUT_ROOT / state["run_id"] / "requirements.txt").exists()


def test_qa_rejects_wrong_interpreter_version(tmp_path, monkeypatch):
    from orchestrator import file_writer
    from orchestrator.qa_runner import qa_python

    monkeypatch.setattr(file_writer, "OUTPUT_ROOT", tmp_path / "output")
    root = tmp_path / "qa_venvs" / ("a" * 32)
    (root / "bin").mkdir(parents=True)
    python = root / "bin/python"
    python.write_text("")
    python.chmod(0o700)
    (root / "pyvenv.cfg").write_text("version = 3.14.6\ninclude-system-site-packages = false\n")
    monkeypatch.delenv("QA_LOCAL_PYTHON", raising=False)
    with pytest.raises(ValueError, match="Python 3.12"):
        qa_python("a" * 32)


def test_profile_completion_is_idempotent_and_preserves_extras():
    from orchestrator.dependencies import complete_fastapi_profile

    original = "# Project dependencies\nfastapi==0.115.12\nuvicorn==0.34.3\npytest==8.4.2\nsqlalchemy==2.0.0"
    completed, additions = complete_fastapi_profile(original)
    assert completed.startswith(original + "\n")
    assert additions == [
        f"{name}=={FASTAPI_PINS[name]}" for name in ("starlette", "pydantic", "httpx", "httpcore")
    ]
    assert complete_fastapi_profile(completed) == (completed, [])


@pytest.mark.parametrize(
    "bad", ["httpx==0.24.1", "httpx>=0.28", "sqlite3", "httpx==0.28.1\nhttpx==0.28.1"]
)
def test_completion_does_not_override_or_hide_invalid_declarations(bad):
    from orchestrator.dependencies import complete_fastapi_profile

    text = "fastapi==0.115.12\n" + bad
    completed, _ = complete_fastapi_profile(text)
    assert completed.startswith(text)
    assert validate_dependencies({"workspace/output_code/requirements.txt": completed})


def test_missing_pins_recovered_before_first_task_persistence(
    tmp_path, monkeypatch, payload, design_payload
):
    from models.developer import DeveloperOutput
    from orchestrator import file_writer
    from orchestrator.development import developer_agent, prepare_development, select_task
    from tests.test_developer import initial, output

    monkeypatch.setattr(file_writer, "OUTPUT_ROOT", tmp_path / "output")
    plan = {
        "tasks": [
            {
                "task_id": "TASK-001",
                "title": "Dependencies",
                "description": "Configure packages",
                "related_user_story_ids": ["US-001"],
                "dependencies": [],
                "expected_artifacts": ["workspace/output_code/requirements.txt"],
                "acceptance_checks": ["Dependencies configured"],
                "status": "pending",
            },
            {
                "task_id": "TASK-002",
                "title": "Application",
                "description": "Implement application",
                "related_user_story_ids": ["US-001"],
                "dependencies": ["TASK-001"],
                "expected_artifacts": ["workspace/output_code/main.py"],
                "acceptance_checks": ["Application implemented"],
                "status": "pending",
            },
        ],
        "shared_artifacts": [],
    }
    state = initial(payload, design_payload, plan)
    state.update(prepare_development(state))
    state.update(select_task(state))
    candidate = DeveloperOutput.model_validate(
        output(state["plan"]["tasks"][0], "fastapi==0.115.12\nuvicorn==0.34.3\npytest==8.4.2\n")
    )
    calls = []

    def generate(context):
        calls.append(context)
        return candidate

    monkeypatch.setattr("orchestrator.development.generate_task", generate)
    state.update(developer_agent(state))
    assert state["plan"]["tasks"][0]["status"] == "completed"
    assert len(calls) == 1 and calls[0]["dependency_profile"] == FASTAPI_PINS
    assert candidate.files[0].content.count("starlette") == 0  # Original LLM object not mutated.
    record = state["development_results"][-1]
    assert len(record["dependency_adjustments"][0]["added_pins"]) == 4
    manifest = (file_writer.OUTPUT_ROOT / state["run_id"] / "requirements.txt").read_text()
    assert not validate_dependencies({"workspace/output_code/requirements.txt": manifest})
    state.update(select_task(state))
    assert state["active_task"] == "TASK-002" and state["status"] == "developing"


def test_profile_is_not_added_for_other_designs(tmp_path, monkeypatch, payload, design_payload):
    import copy

    from models.developer import DeveloperOutput
    from orchestrator import file_writer
    from orchestrator.development import developer_agent, prepare_development, select_task
    from tests.test_developer import initial, output

    design = copy.deepcopy(design_payload)
    design["technology_stack"] = [{"name": "Python", "purpose": "CLI", "rationale": "MVP"}]
    plan = {
        "tasks": [
            {
                "task_id": "T1",
                "title": "Setup",
                "description": "Set up CLI packages",
                "related_user_story_ids": ["US-001"],
                "dependencies": [],
                "expected_artifacts": ["workspace/output_code/requirements.txt"],
                "acceptance_checks": ["CLI dependencies configured"],
                "status": "pending",
            }
        ],
        "shared_artifacts": [],
    }
    monkeypatch.setattr(file_writer, "OUTPUT_ROOT", tmp_path / "output")
    state = initial(payload, design, plan)
    state.update(prepare_development(state))
    state.update(select_task(state))
    data = DeveloperOutput.model_validate(output(state["plan"]["tasks"][0], "pytest==8.4.2\n"))
    monkeypatch.setattr("orchestrator.development.generate_task", lambda context: data)
    result = developer_agent(state)
    assert result["development_results"][-1]["status"] == "completed"
    assert (
        file_writer.OUTPUT_ROOT / state["run_id"] / "requirements.txt"
    ).read_text() == data.files[0].content
