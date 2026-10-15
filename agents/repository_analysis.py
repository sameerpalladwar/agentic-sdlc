"""Bounded, read-only repository analysis; never import source or execute repository commands."""

import ast
import hashlib
import json
import os
from pathlib import Path

from orchestrator.design_validation import redact
from orchestrator.file_writer import reject_symlinks, relative_artifact

EXCLUDED = {
    ".git",
    ".venv",
    "venv",
    "env",
    "__pycache__",
    ".pytest_cache",
    ".ruff_cache",
    "node_modules",
    "workspace",
    "build",
    "dist",
    ".mypy_cache",
    ".tox",
}
EXTENSIONS = {
    ".py",
    ".md",
    ".txt",
    ".toml",
    ".json",
    ".sql",
    ".ini",
    ".cfg",
    ".html",
    ".css",
    ".js",
}
MAX_FILES = 100
MAX_BYTES = 250_000


def inspect_repository(directory: str) -> tuple[dict, dict[str, str]]:
    """Return deterministic metadata and a safe source snapshot scoped to the selected root."""
    root = Path(directory).expanduser().absolute()
    reject_symlinks(root)
    if not root.is_dir():
        raise ValueError("Repository path must be an existing directory.")
    root = root.resolve()
    if not root.stat().st_mode & 0o444 or not root.stat().st_mode & 0o111:
        raise ValueError("Repository directory must be readable and searchable.")
    files: dict[str, str] = {}
    modules = []
    skipped = 0

    def walk_error(error: OSError) -> None:
        raise error

    for current, directories, names in os.walk(root, followlinks=False, onerror=walk_error):
        directories[:] = sorted(
            name
            for name in directories
            if name not in EXCLUDED
            and not name.startswith(".")
            and not (Path(current) / name).is_symlink()
        )
        for name in sorted(names):
            path = Path(current) / name
            relative = path.relative_to(root).as_posix()
            artifact = "workspace/output_code/" + relative
            try:
                if (
                    path.is_symlink()
                    or not path.is_file()
                    or not path.resolve().is_relative_to(root)
                    or path.suffix.lower() not in EXTENSIONS
                ):
                    raise ValueError("Excluded source.")
                relative_artifact(artifact)
            except ValueError:
                skipped += 1
                continue
            with path.open("rb") as source:
                raw = source.read(MAX_BYTES + 1)
            if len(raw) > MAX_BYTES:
                raise ValueError("Repository source exceeds the MVP snapshot budget.")
            try:
                text = raw.decode("utf-8")
            except UnicodeError:
                skipped += 1
                continue
            if "\x00" in text:
                skipped += 1
                continue
            files[artifact] = text
            if (
                len(files) > MAX_FILES
                or sum(len(value.encode()) for value in files.values()) > MAX_BYTES
            ):
                raise ValueError("Repository source exceeds the MVP snapshot budget.")
            if path.suffix == ".py":
                try:
                    tree = ast.parse(text)
                    modules.append(
                        {
                            "path": relative,
                            "classes": [
                                node.name for node in tree.body if isinstance(node, ast.ClassDef)
                            ],
                            "functions": [
                                node.name
                                for node in tree.body
                                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                            ],
                        }
                    )
                except SyntaxError:
                    modules.append(
                        {
                            "path": relative,
                            "syntax_warning": "Source could not be parsed statically.",
                        }
                    )
    if not files or not any(
        Path(path).suffix in {".py", ".js", ".html", ".sql", ".toml"}
        or Path(path).name in {"package.json", "requirements.txt"}
        for path in files
    ):
        raise ValueError("Repository must contain a supported project, not only documentation.")
    if redact(files) != files:
        raise ValueError("Repository contains credential-like source; remove it before analysis.")
    revision = hashlib.sha256(json.dumps(files, sort_keys=True).encode()).hexdigest()
    analysis = {
        "repository_path": str(root),
        "revision": revision,
        "files": sorted(files),
        "python_modules": modules,
        "excluded_files": skipped,
        "source_bytes": sum(len(value.encode()) for value in files.values()),
        "limitations": [
            "Read-only static analysis; source was not imported or executed.",
            "Only supported text files are copied; secrets, environments, caches and binaries are excluded.",
        ],
    }
    return analysis, files


def repository_analysis_agent(state: dict) -> dict:
    """Analyze Brownfield input before Requirements; invalid input remains correctable."""
    try:
        analysis, files = inspect_repository(state["repository_path"])
        return {
            "repository_analysis": analysis,
            "repository_files": files,
            "status": "pending",
            "error": None,
        }
    except (ValueError, OSError, KeyError) as error:
        reason = (
            str(error) if isinstance(error, ValueError) else "Repository source is unavailable."
        )
        return {
            "status": "awaiting_project_input",
            "project_input_kind": "repository",
            "project_questions": [reason + " Supply a corrected repository with --repo-path."],
            "repository_analysis": None,
            "repository_files": {},
            "error": reason,
        }
