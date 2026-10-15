"""Static MVP dependency policy; never installs or imports generated packages."""

import ast
import re
import sys
from pathlib import PurePosixPath

# Reviewed Python 3.12 profile. Pip still checks complete transitive metadata in QA setup.
FASTAPI_PINS = {
    "fastapi": "0.115.12",
    "starlette": "0.46.2",
    "pydantic": "2.12.5",
    "uvicorn": "0.34.3",
    "httpx": "0.28.1",
    "httpcore": "1.0.9",
    "pytest": "8.4.2",
}


def validate_dependencies(files: dict[str, str], *, final: bool = False) -> list[dict]:
    """Return deterministic package/runtime issues from source text without installing or
    importing it.
    """
    issues: list[dict] = []

    def issue(code: str, field: str, message: str) -> None:
        issues.append({"code": code, "field": field, "message": message})

    requirements = next(
        (text for path, text in files.items() if path.endswith("/requirements.txt")), None
    )
    declared: dict[str, str] = {}
    if requirements is not None:
        for line in requirements.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            match = re.fullmatch(r"([A-Za-z0-9_.-]+)==([0-9]+(?:\.[0-9]+)+)", line)
            name = re.split(r"[<>=!~;\s\[]", line)[0].lower().replace("_", "-")
            if name.replace("-", "_") in sys.stdlib_module_names:
                issue(
                    "stdlib_dependency",
                    "requirements.txt",
                    f"{name} belongs to Python, not pip dependencies.",
                )
            elif not match:
                issue(
                    "invalid_dependency",
                    "requirements.txt",
                    f"Use a single exact version pin: {line}",
                )
            elif name in declared:
                issue("duplicate_dependency", "requirements.txt", f"Duplicate dependency: {name}")
            else:
                declared[name] = match[2]
    imports: set[str] = set()
    local = {PurePosixPath(path).parts[2] for path in files if len(PurePosixPath(path).parts) > 2}
    local |= {PurePosixPath(path).stem for path in files if path.endswith(".py")}
    for path, content in files.items():
        if not path.endswith(".py"):
            continue
        tree = ast.parse(content, feature_version=(3, 12))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imports.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                imports.add(node.module.split(".")[0])
    fastapi = "fastapi" in imports or "fastapi" in declared
    if fastapi and (requirements is not None or final):
        for name, version in FASTAPI_PINS.items():
            if declared.get(name) != version:
                issue(
                    "incompatible_dependency" if name in declared else "missing_dependency",
                    "requirements.txt",
                    f"Python 3.12 MVP profile requires {name}=={version}.",
                )
    if final:
        aliases = {"yaml": "pyyaml", "PIL": "pillow", "dotenv": "python-dotenv"}
        for name in sorted(imports - sys.stdlib_module_names - local):
            distribution = aliases.get(name, name.replace("_", "-"))
            if distribution not in declared:
                issue(
                    "missing_dependency",
                    "requirements.txt",
                    f"Imported package {name} is not declared.",
                )
        if fastapi:
            readme = next((v for k, v in files.items() if k.endswith("/README.md")), "")
            if not re.search(r"Python\s+3\.12\b", readme, re.I):
                issue(
                    "missing_runtime",
                    "README.md",
                    "Document Python 3.12 as the application runtime.",
                )
            if any(
                version != "3.12" for version in re.findall(r"Python\s+(\d+\.\d+)\b", readme, re.I)
            ):
                issue(
                    "unsupported_python",
                    "README.md",
                    "Only Python 3.12 is supported for generated MVP applications.",
                )
    return issues
