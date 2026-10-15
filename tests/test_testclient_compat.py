"""Deterministic compatibility repairs preserve tests and require exact runtime evidence."""

import ast

import pytest
from langgraph.checkpoint.sqlite import SqliteSaver

from models.developer import DeveloperOutput
from models.qa import QAResult
from orchestrator import file_writer
from orchestrator.dependencies import FASTAPI_PINS
from orchestrator.file_writer import SafeWorkspace
from orchestrator.graph import build_graph
from orchestrator.qa_recovery import repair_testclient
from orchestrator.testclient_compat import compatibility_changes
from tests.conftest import approved_review
from tests.test_qa import completed_state

RUN = "a" * 32
PATH = "workspace/output_code/test_app.py"
SOURCE = """from fastapi.testclient import TestClient
client = TestClient(app)

def test_redirect():
    note = "allow_redirects=False stays in this literal"
    response = client.get("/abc", allow_redirects=False)  # preserve this comment
    assert response.status_code == 307
"""


def feedback(source=SOURCE, method="get", path="test_app.py", line=None):
    if line is None:
        node = next(
            node
            for node in ast.walk(ast.parse(source))
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == method
        )
        line = node.lineno
    return {
        "category": "test_failure",
        "failing_tests": [path + "::test_redirect"],
        "traceback_summary": f"E TypeError: TestClient.{method}() got an unexpected keyword argument 'allow_redirects'\n\n{path}:{line}: TypeError",
        "error_summary": "pytest failed",
    }


def test_only_reported_keyword_changes():
    changes = compatibility_changes({PATH: SOURCE}, feedback())
    assert len(changes) == 1
    expected = SOURCE.replace(
        'client.get("/abc", allow_redirects=False)', 'client.get("/abc", follow_redirects=False)'
    )
    assert changes[0].content == expected
    assert "assert response.status_code == 307" in changes[0].content
    assert compatibility_changes({PATH: expected}, feedback()) == []


@pytest.mark.parametrize(
    "source",
    [
        SOURCE.replace("client = TestClient(app)", "client = OtherClient(app)"),
        SOURCE.replace(
            "client = TestClient(app)", "client = TestClient(app)\nclient = OtherClient(app)"
        ),
        SOURCE.replace("def test_redirect():", "def test_redirect(client):"),
        SOURCE.replace("from fastapi.testclient", "from httpx"),
        SOURCE.replace(
            "client = TestClient(app)", "TestClient = OtherClient\nclient = TestClient(app)"
        ),
        SOURCE.replace("allow_redirects=False)  #", "allow_redirects=dynamic)  #"),
        SOURCE.replace(
            "allow_redirects=False)  #", "allow_redirects=False, follow_redirects=True)  #"
        ),
        SOURCE.replace("allow_redirects=False)  #", "allow_redirects=False, **options)  #"),
    ],
)
def test_ambiguous_or_unsafe_calls_not_rewritten(source):
    assert compatibility_changes({PATH: source}, feedback(source)) == []


@pytest.mark.parametrize("change", ["category", "error", "line", "path", "file", "failed_name"])
def test_evidence_required(change):
    data = feedback()
    files = {PATH: SOURCE}
    if change == "category":
        data["category"] = "execution_error"
    elif change == "error":
        data["traceback_summary"] = "AssertionError: expected 307"
    elif change == "line":
        data = feedback(line=1)
    elif change == "path":
        data = feedback(path="../test_app.py")
    elif change == "file":
        files = {}
    else:
        data["failing_tests"] = ["other_test.py::test_redirect"]
    assert compatibility_changes(files, data) == []


def test_alias_and_unicode_multiline_offsets():
    source = """from starlette.testclient import TestClient as TC
client = TC(app)
def test_redirect():
    note = "é allow_redirects=False"
    response = client.get(
        "/abc",  # unchanged
        allow_redirects=False,
    )
    assert response.status_code == 307
"""
    changes = compatibility_changes({PATH: source}, feedback(source))
    assert changes[0].content == source.replace(
        "        allow_redirects=False,", "        follow_redirects=False,"
    )


@pytest.fixture
def prepared(tmp_path, monkeypatch, payload, design_payload):
    monkeypatch.setattr(file_writer, "OUTPUT_ROOT", tmp_path / "output")
    state = completed_state(payload, design_payload, RUN)
    # Trusted, statically inspected source text; this fixture never imports generated modules.
    requirements = "\n".join(f"{name}=={version}" for name, version in FASTAPI_PINS.items())
    output = DeveloperOutput(
        task_id="trusted-compatibility-fixture",
        files=[
            {"path": PATH, "content": SOURCE},
            {"path": "workspace/output_code/requirements.txt", "content": requirements},
            {"path": "workspace/output_code/README.md", "content": "Python 3.12"},
        ],
        implementation_summary="Trusted fixture for compatibility recovery",
        acceptance_check_mapping=[
            {"check": "Redirect behavior", "implementation": "Assertions retained"}
        ],
        assumptions=[],
        warnings=[],
        completion_status="completed",
    )
    with SafeWorkspace(RUN) as workspace:
        workspace.persist(output, "trusted-fixture")
    state.update(qa_local_authorized=True, through="delivery", recovery_history=[])
    monkeypatch.setattr(
        "orchestrator.qa_recovery.generate_task",
        lambda c: pytest.fail("Compatibility repair must not call the LLM"),
    )
    return state


def test_receipt_replay_does_not_repeat_changes(prepared):
    with SafeWorkspace(RUN) as workspace:
        first = repair_testclient(workspace, workspace.snapshot(), feedback(), 1)
    with SafeWorkspace(RUN) as workspace:
        second = repair_testclient(workspace, workspace.snapshot(), feedback(), 1)
    assert first == second
    assert first.files[0].content.count("follow_redirects=False") == 1


@pytest.mark.parametrize("restart", [False, True])
def test_graph_reruns_both_branches_same_run_and_waits_for_approval(
    prepared, tmp_path, monkeypatch, restart
):
    reviews, reports = [], []

    def qa(run_id, files, **kwargs):
        failed = 'client.get("/abc", allow_redirects=False)' in files[PATH]
        reports.append(failed)
        return QAResult(
            status="qa_failed" if failed else "qa_passed",
            exit_code=1 if failed else 0,
            total_tests=1,
            passed_tests=0 if failed else 1,
            failed_tests=1 if failed else 0,
            test_output=(
                feedback()["traceback_summary"] + "\nFAILED test_app.py::test_redirect - TypeError"
            )
            if failed
            else "1 passed",
        )

    def review(context):
        reviews.append(context["revision"])
        return approved_review(context)

    monkeypatch.setattr("agents.qa.run_tests", qa)
    monkeypatch.setattr("agents.reviewer.generate_review", review)
    config = {"configurable": {"thread_id": RUN}}
    database = str(tmp_path / "checkpoint.sqlite")
    original = SafeWorkspace.persist

    def crash_after_persistence(self, output, fingerprint):
        result = original(self, output, fingerprint)
        if output.task_id.startswith("qa-testclient-compatibility-"):
            raise RuntimeError("Simulated process exit before repair checkpoint")
        return result

    with SqliteSaver.from_conn_string(database) as saver:
        graph = build_graph(saver)
        graph.update_state(config, prepared, as_node="development_select")
        if restart:
            monkeypatch.setattr(SafeWorkspace, "persist", crash_after_persistence)
            with pytest.raises(RuntimeError):
                graph.invoke(None, config)
            monkeypatch.setattr(SafeWorkspace, "persist", original)
        else:
            graph.invoke(None, config)
    with SqliteSaver.from_conn_string(database) as saver:
        graph = build_graph(saver)
        result = graph.invoke(None, config) if restart else graph.get_state(config).values
    assert reports == [True, False] and len(reviews) == 2 and reviews[0] != reviews[1]
    assert result["status"] == "awaiting_approval" and result["run_id"] == RUN
    assert result["qa_repair_count"] == 1
    assert result["qa_result"]["revision"] == result["review_result"]["reviewed_revision"]
    assert not result.get("final_manifest")
    event = next(
        h
        for h in result["recovery_history"]
        if h.get("repair_kind") == "testclient_keyword_compatibility"
    )
    assert event["changed_files"] == [PATH] and event["outcome"] == "applied"


@pytest.mark.parametrize("condition", ["no_authorization", "exhausted"])
def test_blockers_do_not_modify_tests(prepared, tmp_path, monkeypatch, condition):
    if condition == "no_authorization":
        prepared["qa_local_authorized"] = False
    else:
        prepared["qa_repair_count"] = 2

    def qa(*args, **kwargs):
        if not kwargs.get("local_authorized"):
            return QAResult(status="qa_blocked", error_summary="Authorization required")
        return QAResult(
            status="qa_failed",
            exit_code=1,
            total_tests=1,
            failed_tests=1,
            test_output=feedback()["traceback_summary"]
            + "\nFAILED test_app.py::test_redirect - TypeError",
        )

    monkeypatch.setattr("agents.qa.run_tests", qa)
    monkeypatch.setattr("agents.reviewer.generate_review", approved_review)
    with SqliteSaver.from_conn_string(str(tmp_path / "checkpoint.sqlite")) as saver:
        graph = build_graph(saver)
        config = {"configurable": {"thread_id": RUN}}
        graph.update_state(config, prepared, as_node="development_select")
        result = graph.invoke(None, config)
    with SafeWorkspace(RUN) as workspace:
        assert workspace.snapshot()[PATH] == SOURCE
    assert result["status"] == (
        "validation_blocked" if condition == "no_authorization" else "validation_failed"
    )
