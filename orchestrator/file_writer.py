"""Conservative run-local writer. Never imports or executes generated code."""
import ast
import fcntl
import hashlib
import json
import os
import re
import tempfile
from pathlib import Path, PurePosixPath

from models.developer import DeveloperOutput
from models.planning import validate_artifact_path


OUTPUT_ROOT = Path(__file__).resolve().parents[1] / "workspace" / "output_code"
MAX_FILE_BYTES = 500_000
MAX_TOTAL_BYTES = 2_000_000


def digest(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def relative_artifact(path: str) -> Path:
    validate_artifact_path(path)
    relative = PurePosixPath(path).parts[2:]
    for part in relative:
        lower = part.lower()
        if (not re.fullmatch(r"[A-Za-z0-9_.-]+", part) or
                part.startswith(".") and part != ".gitignore" or part.endswith(".") or
                lower.split(".")[0] in {"con", "prn", "aux", "nul", *[f"com{i}" for i in range(1, 10)], *[f"lpt{i}" for i in range(1, 10)]} or
                any(word in lower for word in ("credential", "secret", "private_key", "id_rsa", "token", "password", "api_key", "apikey")) or
                lower.endswith((".pem", ".key", ".p12", ".pfx"))):
            raise ValueError("Unsafe or sensitive generated file name.")
    if Path(relative[-1]).suffix.lower() not in {".py", ".md", ".txt", ".toml", ".json", ".sql", ".ini", ".cfg", ".html", ".css", ".js"} and relative[-1] != ".gitignore":
        raise ValueError("Unsupported generated file type.")
    return Path(*relative)


def reject_symlinks(path: Path) -> None:
    for candidate in [path, *path.parents]:
        if candidate.is_symlink():
            raise ValueError("Symlinks are not permitted in generated workspace paths.")


def atomic_write(path: Path, content: str) -> None:
    reject_symlinks(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".write-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        reject_symlinks(path)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


class SafeWorkspace:
    """Use a write-ahead journal and task receipts for safe replay after checkpoint loss."""
    def __init__(self, run_id: str):
        if not re.fullmatch(r"[a-f0-9]{32}", run_id):
            raise ValueError("Invalid output run ID.")
        self.root = OUTPUT_ROOT / run_id
        reject_symlinks(self.root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.manifest_path = self.root / ".developer-state.json"
        self.lock_path = self.root / ".developer.lock"

    def __enter__(self):
        reject_symlinks(self.lock_path)
        self.lock = self.lock_path.open("a")
        try:
            fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            reject_symlinks(self.manifest_path)
            self.manifest = json.loads(self.manifest_path.read_text()) if self.manifest_path.exists() else {"files": {}, "receipts": {}, "pending": None}
        except Exception:
            self.lock.close()
            raise
        return self

    def __exit__(self, *args):
        self.lock.close()

    def target(self, path: str) -> Path:
        target = self.root / relative_artifact(path)
        reject_symlinks(target)
        if not target.resolve().is_relative_to(self.root.resolve()):
            raise ValueError("Generated path escapes the run workspace.")
        return target

    def snapshot(self) -> dict[str, str]:
        """Read only managed files; detect manual changes before generating more code."""
        snapshot: dict[str, str] = {}
        pending = self.manifest.get("pending")
        pending_files = {f["path"]: digest(f["content"]) for f in pending["output"]["files"]} if pending else {}
        for path, record in self.manifest["files"].items():
            target = self.target(path)
            if not target.is_file():
                raise ValueError("A managed generated file is missing.")
            content = target.read_text(encoding="utf-8")
            if digest(content) not in {record["hash"], pending_files.get(path)}:
                raise ValueError("A generated file was modified outside the harness.")
            snapshot[path] = content
        if sum(len(c.encode()) for c in snapshot.values()) > MAX_TOTAL_BYTES:
            raise ValueError("Generated workspace exceeds the context size budget.")
        return snapshot

    def receipt(self, task_id: str, fingerprint: str) -> DeveloperOutput | None:
        self.snapshot()
        pending = self.manifest.get("pending")
        if pending:
            if pending["task_id"] != task_id or pending["fingerprint"] != fingerprint:
                raise ValueError("An unfinished write belongs to another task or input.")
            output = DeveloperOutput.model_validate(pending["output"])
            self.persist(output, fingerprint)
        receipt = self.manifest["receipts"].get(task_id)
        if receipt:
            if receipt["fingerprint"] != fingerprint:
                raise ValueError("Task inputs changed after generated files were saved.")
            return DeveloperOutput.model_validate(receipt["output"])
        return None

    def persist(self, output: DeveloperOutput, fingerprint: str) -> list[str]:
        if self.manifest.get("validation_revision"):
            raise ValueError("Developer writes are blocked during validation.")
        paths = [file.path for file in output.files]
        if len(paths) != len(set(paths)):
            raise ValueError("Duplicate generated paths.")
        if sum(len(f.content.encode()) for f in output.files) > MAX_TOTAL_BYTES:
            raise ValueError("Generated task exceeds the output size budget.")
        if len(output.files) > 100:
            raise ValueError("Too many files in a single task output.")
        pending = self.manifest.get("pending")
        recovery = bool(pending and pending["task_id"] == output.task_id and pending["fingerprint"] == fingerprint and pending["output"] == output.model_dump())
        # Validate the whole response and every destination before any application write.
        targets = []
        for file in output.files:
            target = self.target(file.path)
            for parent in target.parents:
                if parent == self.root:
                    break
                if parent.exists() and not parent.is_dir():
                    raise ValueError("Generated parent path is not a directory.")
            if any(target in previous.parents or previous in target.parents for _, previous in targets):
                raise ValueError("Generated file paths conflict with directory paths.")
            if len(file.content.encode()) > MAX_FILE_BYTES:
                raise ValueError("Generated file exceeds the size budget.")
            if target.suffix == ".py":
                ast.parse(file.content, filename=str(target))
            if target.exists():
                if not target.is_file() or target.stat().st_nlink != 1:
                    raise ValueError("Generated destination must be a regular unlinked file.")
                record = self.manifest["files"].get(file.path)
                actual = digest(target.read_text(encoding="utf-8"))
                permitted = {record["hash"]} if record else set()
                if recovery:
                    permitted.add(digest(file.content))
                if actual not in permitted:
                    raise ValueError("Refusing to overwrite an unrelated or modified file.")
            targets.append((file, target))
        self.manifest["pending"] = {"task_id": output.task_id, "fingerprint": fingerprint, "output": output.model_dump()}
        atomic_write(self.manifest_path, json.dumps(self.manifest))
        for file, target in targets:
            atomic_write(target, file.content)
            self.manifest["files"][file.path] = {"hash": digest(file.content), "task_id": output.task_id}
        self.manifest["receipts"][output.task_id] = {"fingerprint": fingerprint, "output": output.model_dump()}
        self.manifest["pending"] = None
        atomic_write(self.manifest_path, json.dumps(self.manifest))
        return [str(target) for _, target in targets]
