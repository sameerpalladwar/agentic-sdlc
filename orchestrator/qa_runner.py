"""Local pytest for explicitly approved code. This is not a security sandbox."""
import os
import re
import subprocess
import tempfile
import time
from pathlib import Path

from models.qa import QAResult
from orchestrator import file_writer
from orchestrator.file_writer import reject_symlinks, relative_artifact


COMMAND = ("python", "-m", "pytest", "-q")
TIMEOUT_SECONDS = 60
OUTPUT_LIMIT = 32_768
FILE_LIMIT = 1_000_000


def generated_directory(run_id: str, application_dir: str | None = None) -> Path:
    if not re.fullmatch(r"[a-f0-9]{32}", run_id):
        raise ValueError("Invalid generated application run ID.")
    expected = file_writer.OUTPUT_ROOT / run_id
    path = Path(application_dir) if application_dir else expected
    reject_symlinks(path)
    if path.absolute() != expected.absolute() or not path.is_dir():
        raise ValueError("Application directory must be the existing output workspace for this run.")
    if not path.resolve().is_relative_to(file_writer.OUTPUT_ROOT.resolve()):
        raise ValueError("Application directory escapes the configured output workspace.")
    return path


def qa_python(run_id: str) -> Path:
    root = file_writer.OUTPUT_ROOT.parent / "qa_venvs" / run_id
    expected = root / "bin" / "python"
    configured = Path(os.getenv("QA_LOCAL_PYTHON", str(expected)))
    reject_symlinks(root)
    reject_symlinks(expected.parent)
    config = root / "pyvenv.cfg"
    reject_symlinks(config)
    if configured.absolute() != expected.absolute() or not configured.is_absolute():
        raise ValueError("QA_LOCAL_PYTHON must name workspace/qa_venvs/<run_id>/bin/python.")
    if not config.is_file() or not expected.is_file() or not os.access(expected, os.X_OK):
        raise ValueError("Create the dedicated QA venv and manually install reviewed dependencies first.")
    if not re.search(r"(?m)^version\s*=\s*3\.12\.", config.read_text()):
        raise ValueError("Generated applications require a Python 3.12 QA environment.")
    if not re.search(r"(?im)^include-system-site-packages\s*=\s*false\s*$", config.read_text()):
        raise ValueError("The dedicated QA venv must not expose system site packages.")
    return expected  # Python itself may be the usual venv symlink to the base interpreter.


def clean_environment(root: Path) -> dict[str, str]:
    # Construct an allowlist rather than trying to enumerate every secret variable.
    return {"PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": str(root), "TMPDIR": str(root),
            "PYTHONPATH": str(root), "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
            "PYTHONDONTWRITEBYTECODE": "1", "NO_COLOR": "1", "TERM": "dumb", "LANG": "C.UTF-8"}


def child_limits() -> None:
    import resource
    resource.setrlimit(resource.RLIMIT_FSIZE, (FILE_LIMIT, FILE_LIMIT))
    resource.setrlimit(resource.RLIMIT_CPU, (TIMEOUT_SECONDS, TIMEOUT_SECONDS))
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))


def parse_pytest(code: int, output: str, duration: float) -> QAResult:
    """Use only pytest's final summary. No summary means no invented test counts."""
    summary = next((line for line in reversed(output.splitlines())
                    if re.search(r"\bin \d+(?:\.\d+)?s\b", line)), "")
    counts = {name: int(number) for number, name in re.findall(
        r"\b(\d+) (passed|failed|skipped|errors?|xfailed|xpassed|deselected)\b", summary)}
    passed = counts.get("passed", 0) + counts.get("xpassed", 0)
    failed = counts.get("failed", 0) + counts.get("error", 0) + counts.get("errors", 0)
    skipped = counts.get("skipped", 0) + counts.get("xfailed", 0)
    total = passed + failed + skipped
    if code == 5:
        status, error = "qa_blocked", "No tests collected. Add pytest tests before QA can pass."
    elif code != 0:
        status, error = "qa_failed", "Pytest failed; inspect test_output for assertion or collection errors."
    elif total == 0 or passed + failed == 0:
        status, error = "qa_blocked", "No passing or failing tests were observed; QA cannot claim success."
    elif failed:
        status, error = "qa_failed", "Pytest reported failures despite a zero exit code."
    else:
        status, error = "qa_passed", ""
    return QAResult(status=status, exit_code=code, total_tests=total, passed_tests=passed,
                    failed_tests=failed, skipped_tests=skipped, duration_seconds=duration,
                    error_summary=error, test_output=output)


def run_tests(run_id: str, files: dict[str, str], command: tuple[str, ...] = COMMAND,
              *, local_authorized: bool = False, application_dir: str | None = None) -> QAResult:
    if tuple(command) != COMMAND:
        raise ValueError("Only python -m pytest -q is approved.")
    try:
        generated_directory(run_id, application_dir)
        if not local_authorized:
            return QAResult(status="qa_blocked", error_summary="Explicit developer approval is required. Review the code, then use qa --run-id <id> --authorize-local-execution. A venv is not a sandbox.")
        python = qa_python(run_id)
        if os.name != "posix":
            return QAResult(status="qa_blocked", error_summary="This local MVP requires POSIX resource-limit support.")
        with tempfile.TemporaryDirectory(prefix="reviewed-pytest-") as temporary:
            root = Path(temporary)
            app = root / "app"
            app.mkdir()
            for path, content in files.items():
                target = app / relative_artifact(path)
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(content, encoding="utf-8")
            # Avoid discovering the parent harness's tests or unreviewed pytest options.
            (app / "pytest.ini").write_text("[pytest]\naddopts =\npythonpath = .\ntestpaths = .\n")
            started = time.monotonic()
            with tempfile.TemporaryFile() as stdout, tempfile.TemporaryFile() as stderr:
                timed_out = False
                try:
                    result = subprocess.run([str(python), *COMMAND[1:]], cwd=app,
                        env=clean_environment(root), shell=False, timeout=TIMEOUT_SECONDS,
                        stdout=stdout, stderr=stderr, preexec_fn=child_limits)
                    code = result.returncode
                except subprocess.TimeoutExpired:
                    timed_out, code = True, None
                stdout.seek(0)
                stderr.seek(0)
                out, err = stdout.read(OUTPUT_LIMIT + 1), stderr.read(OUTPUT_LIMIT + 1)
            duration = time.monotonic() - started
            text = out[:OUTPUT_LIMIT].decode("utf-8", errors="replace")
            if err:
                text += "\nSTDERR:\n" + err[:OUTPUT_LIMIT].decode("utf-8", errors="replace")
            if timed_out:
                return QAResult(status="qa_failed", duration_seconds=duration, test_output=text, error_summary="Pytest timed out; partial results are not treated as a pass.")
            if len(out) > OUTPUT_LIMIT or len(err) > OUTPUT_LIMIT:
                return QAResult(status="qa_failed", exit_code=code, duration_seconds=duration, test_output=text, error_summary="Test output exceeded the capture limit; counts are unverified.")
            return parse_pytest(code, text, duration)
    except (ValueError, OSError, subprocess.SubprocessError) as exc:
        return QAResult(status="qa_blocked", error_summary="QA execution unavailable: " + str(exc))
