import json

import pytest

import run


@pytest.mark.parametrize("clarification", [False, True])
def test_cli_saves_json(tmp_path, monkeypatch, capsys, llm_response, payload, clarification):
    if clarification:
        payload.update(requires_human_clarification=True, open_questions=["Which users?"])
    monkeypatch.setattr(run, "RUNS_DIR", tmp_path / "runs")
    assert run.main(["--requirement", "Build a URL shortener"]) == 0
    captured = capsys.readouterr()
    assert json.loads(captured.out) == payload
    files = list((tmp_path / "runs").glob("*/requirements.json"))
    assert len(files) == 1
    assert json.loads(files[0].read_text()) == payload
    assert str(files[0]) in captured.err
    assert run.main(["--requirement", "Build another shortener"]) == 0
    assert len(list((tmp_path / "runs").glob("*/requirements.json"))) == 2


def test_cli_failure_does_not_save(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(run, "RUNS_DIR", tmp_path / "runs")
    assert run.main(["--requirement", " "]) == 1
    captured = capsys.readouterr()
    assert json.loads(captured.err)["status"] == "failed"
    assert captured.out == ""
    assert not (tmp_path / "runs").exists()


def test_cli_requires_argument():
    with pytest.raises(SystemExit) as error:
        run.main([])
    assert error.value.code == 2


def test_save_failure(tmp_path, monkeypatch, capsys, llm_response):
    blocked = tmp_path / "file"
    blocked.write_text("existing")
    monkeypatch.setattr(run, "RUNS_DIR", blocked)
    assert run.main(["--requirement", "Shorten URLs"]) == 1
    assert "Unable to save" in capsys.readouterr().err
