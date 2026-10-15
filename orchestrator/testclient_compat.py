"""Evidence-bound TestClient keyword repair; no LLM, assertion edits, or test execution."""

import ast
import re
from collections import Counter

from models.developer import FileChange

METHODS = {"get", "post", "put", "patch", "delete", "head", "options", "request"}


def compatibility_changes(files: dict[str, str], feedback: dict) -> list[FileChange]:
    """Rename only a reported failing keyword on a proven, unshadowed TestClient receiver."""
    if feedback.get("category") != "test_failure":
        return []
    failing = {name.split("::")[0] for name in feedback.get("failing_tests", [])}
    evidence: dict[str, set[tuple[int, str]]] = {}
    pattern = (
        r"TypeError: TestClient\.(\w+)\(\) got an unexpected keyword argument ['\"]allow_redirects['\"]"
        r"\s*\n\s*([A-Za-z0-9_./-]+\.py):(\d+): TypeError"
    )
    for method, path, line in re.findall(pattern, feedback.get("traceback_summary", "")):
        if method in METHODS and path in failing:
            evidence.setdefault("workspace/output_code/" + path, set()).add((int(line), method))
    changes = []
    for path, locations in evidence.items():
        if path not in files or not is_test_path(path):
            continue
        content = files[path]
        tree = ast.parse(content, feature_version=(3, 12))
        clients = proven_clients(tree)
        raw = content.encode("utf-8")
        offsets = [0]
        for line in raw.splitlines(keepends=True):
            offsets.append(offsets[-1] + len(line))
        replacements = set()
        for node in ast.walk(tree):
            if not (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id in clients
                and node.func.attr in METHODS
                and any(
                    node.lineno <= line <= node.end_lineno and method == node.func.attr
                    for line, method in locations
                )
            ):
                continue
            if any(
                keyword.arg == "follow_redirects" or keyword.arg is None
                for keyword in node.keywords
            ):
                continue
            for keyword in node.keywords:
                if (
                    keyword.arg == "allow_redirects"
                    and isinstance(keyword.value, ast.Constant)
                    and type(keyword.value.value) is bool
                ):
                    start = offsets[keyword.lineno - 1] + keyword.col_offset
                    if raw[start : start + len(b"allow_redirects")] != b"allow_redirects":
                        continue
                    replacements.add(start)
                    keyword.arg = "follow_redirects"
        for start in sorted(replacements, reverse=True):
            raw = raw[:start] + b"follow_redirects" + raw[start + len(b"allow_redirects") :]
        if replacements:
            updated = raw.decode("utf-8")
            # The expected tree differs only in keyword names. Assertions, values and guards stay exact.
            if ast.dump(ast.parse(updated), include_attributes=False) != ast.dump(
                tree, include_attributes=False
            ):
                raise ValueError("Compatibility repair changed more than keyword names.")
            changes.append(FileChange(path=path, content=updated))
    return changes


def is_test_path(path: str) -> bool:
    from pathlib import PurePosixPath

    name = PurePosixPath(path).name
    return (
        path.startswith("workspace/output_code/")
        and ".." not in PurePosixPath(path).parts
        and (
            "tests" in PurePosixPath(path).parts
            or name.startswith("test_")
            or name.endswith("_test.py")
        )
    )


def proven_clients(tree: ast.Module) -> set[str]:
    """Support direct module-level TestClient assignments; ambiguous bindings require review."""
    imports = [
        node
        for node in tree.body
        if isinstance(node, ast.ImportFrom)
        and node.level == 0
        and node.module in {"fastapi.testclient", "starlette.testclient"}
    ]
    constructors = {
        alias.asname or alias.name
        for node in imports
        for alias in node.names
        if alias.name == "TestClient"
    }
    stores = Counter(
        node.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del))
    )
    arguments = {node.arg for node in ast.walk(tree) if isinstance(node, ast.arg)}
    definitions = {
        node.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
    }
    imported = Counter(
        alias.asname or alias.name.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, (ast.Import, ast.ImportFrom))
        for alias in node.names
    )
    constructors = {
        name
        for name in constructors
        if not stores[name] and name not in arguments | definitions and imported[name] == 1
    }
    return {
        node.targets[0].id
        for node in tree.body
        if isinstance(node, ast.Assign)
        and len(node.targets) == 1
        and isinstance(node.targets[0], ast.Name)
        and isinstance(node.value, ast.Call)
        and isinstance(node.value.func, ast.Name)
        and node.value.func.id in constructors
        and stores[node.targets[0].id] == 1
        and node.targets[0].id not in arguments | definitions
        and not imported[node.targets[0].id]
    }
