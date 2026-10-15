# Engineering Guidelines

## Project
Standalone multi-agent SDLC harness using Python and LangGraph.

## Coding Standards
- Prefer simple, readable Python.
- Use type hints.
- Keep functions small and focused.
- Avoid unnecessary abstractions.
- Write pytest tests for new functionality.

## Architecture
- LangGraph controls workflow execution.
- Agents perform specialized engineering tasks.
- Shared state is defined in orchestrator/state.py.
- Generated code belongs in workspace/output_code/.

## Safety
- Never execute arbitrary LLM-generated shell commands.
- Require approval for high-impact changes.
- Restrict file writes to approved workspace paths.

## Workflow and Module Responsibilities
- `agents/` contains focused OpenAI adapters; Markdown instructions live in `prompts/`.
- `models/` defines structured requirements, design, planning, developer, QA and review contracts.
- `orchestrator/graph.py` owns routing and explicit parallel fan-in. QA and Review write separate keys.
- `revision.py` freezes managed code; `file_writer.py` journals writes and enforces workspace barriers.
- `validation.py` combines evidence; `qa_recovery.py` bounds automatic source repairs to two per run.
- `approval.py` binds human decisions to validated revisions and produces final delivery metadata.
- `artifacts.py` exports checkpoints as artifacts; `observability.py` logs metadata, never raw state.
- `progress.py` serializes terminal output for parallel branches; `run.py` owns CLI and checkpoint I/O.

```text
Requirements -> Clarification? -> Design/Validator -> Planning -> Developer
  -> Snapshot -> [QA || Review] -> Validation
  failure -> bounded repair -> Snapshot
  pass -> stop at validation OR Human Approval -> Final Delivery
  human changes -> source repair -> Snapshot -> fresh approval
```

## Development Conventions
- Harness Python is 3.11+; generated MVP applications and production QA environments target Python 3.12.
- Install development tools with `pip install -r requirements-dev.txt`.
- Format with `ruff format .`; lint with `ruff check .`; run `python -m pytest -q`.
- Ruff uses 100-column formatting and minimal import/error checks. Keep style-only edits mechanical.
- Document nontrivial routing, replay semantics and security boundaries; avoid restating obvious code.
- Shared test fixtures live in `tests/conftest.py`. Use mocked SDK responses and trusted fixture code.

## Approval and Observability Safety
- Use one CLI operation per run. Checkpoints, not logs, are the workflow source of truth.
- Human approval must match current live source, QA and Review revision IDs and attempt numbers.
- Requesting changes invalidates validation and requires fresh QA, Review and human approval.
- Never let an LLM auto-edit tests or weaken guards. The sole automatic test-edit exception is
  the deterministic, evidence-bound TestClient `allow_redirects` -> `follow_redirects` policy;
  preserve assertions and values, consume the existing repair budget, and rerun QA and Review.
- Never install generated dependencies, deploy or publish generated code without authorization.
- Production local QA requires explicit authorization and its dedicated Python 3.12 environment.
- A virtual environment is not a security sandbox; fixture-only interpreter overrides belong only in tests.
- Keep secrets, raw LLM/state payloads and human feedback out of logs. Log safe metadata and stack frames only.
- Per-run artifacts/logs belong in `workspace/runs/<run_id>/`; generated applications remain separate.
