import pytest

from agents.prompt_loader import load_prompt


def test_prompt_from_another_directory(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert "# Requirements Agent" in load_prompt()


def test_custom_prompt(tmp_path):
    path = tmp_path / "prompt.md"
    path.write_text("  Custom instructions\n", encoding="utf-8")
    assert load_prompt(path) == "Custom instructions"


def test_empty_prompt(tmp_path):
    path = tmp_path / "prompt.md"
    path.write_text(" ")
    with pytest.raises(ValueError):
        load_prompt(path)


def test_missing_prompt(tmp_path):
    with pytest.raises(FileNotFoundError):
        load_prompt(tmp_path / "missing.md")
