# Agentic SDLC Harness

LangGraph harness for an agentic software development lifecycle workflow.
Stage 1 converts natural-language requirements into structured user stories using OpenAI.
Stage 2 pauses ambiguous runs for human clarification and resumes from persistent checkpoints.
Stage 3 optionally converts completed requirements into a reviewable technical design.

## Architecture Overview

![Agentic SDLC architecture showing the LangGraph orchestrator, specialized agents, shared state, and generated application](docs/images/architecture.png)

## Setup

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

Set `OPENAI_API_KEY` and `OPENAI_MODEL` in `.env`. Existing environment variables
take precedence. Use a model that supports structured outputs; the example uses
`gpt-4.1-mini`. Never commit credentials. Python 3.11 or later is required.

## Run

```bash
python run.py start --requirement "Build a URL shortener service"
```

The CLI prints a JSON report with `run_id`, `thread_id`, `status`, `requirements`,
and clarification history. Each start creates a unique run ID that remains the
LangGraph thread ID throughout the run. The Stage 1 `--requirement` shorthand still works.

Ambiguous requirements produce `status: "paused"` and `open_questions`. The command
exits immediately without prompting for terminal input, making it suitable for scripts.
Answer the questions in plain text and resume using the returned run ID:

```bash
python run.py resume --run-id <run_id> --answers "Public users need to shorten URLs and follow short links."
```

Answers are stored with the questions and passed back to the Requirements Agent
alongside the original request. The agent regenerates validated requirements. If
blocking ambiguity remains, the same run pauses again. To reject a paused run:

```bash
python run.py resume --run-id <run_id> --reject
```

Rejection ends the run with `rejected`, preserves the draft, and makes no further
LLM call. Completed, failed, rejected, and unknown runs cannot be resumed with
these commands. Blank answers are rejected without consuming the pause.

Under `workspace/runs/<run_id>/`, `checkpoints.sqlite` stores durable graph state,
`requirements.json` stores the latest requirements object, and `status.json` stores
the latest CLI report. Resume works from another process using the same local files;
legacy Stage 1 artifacts without checkpoints cannot be resumed. A failed regeneration
clears requirements in state and writes `null` over the old requirements artifact.
Checkpoint history retains previous drafts and answers. Run artifacts are ignored
by Git; keep them private and do not edit checkpoints manually.

Exit codes: 0 for completion, pause, or rejection; 1 for workflow, storage, or invalid
resume errors; 2 for invalid CLI arguments. Workflow failures appear in the JSON
report; CLI/storage errors go to stderr. No generated commands are executed.
Use one CLI operation at a time per run; this MVP does not coordinate concurrent resumes.

## Requirements Agent

Instructions live in `prompts/requirements.md` and are loaded relative to the
repository, independently of the working directory. Pydantic models in
`models/requirements.py` validate the OpenAI SDK structured output.

The result includes `summary`, `user_stories`, `non_functional_requirements`,
`assumptions`, `open_questions`, and `requires_human_clarification`. Each story
has an ID, title, `as_a`, `i_want`, `so_that`, MoSCoW priority (`must`, `should`,
`could`, `wont`), and acceptance criteria with `given`, `when`, and `then` fields.
Blocking ambiguity produces targeted questions rather than invented requirements.

The graph starts with `pending` and stores validated data in
`state["requirements"]`. Routing after the requirements node is conditional:

- `requirements_complete` goes to `END` by default, or to the Design Agent when
  the run was started with `--through design`.
- `needs_human_clarification` goes through a pause node, which persists `paused`,
  and then a clarification node calls LangGraph `interrupt()` with the open questions.
- `Command(resume={"answers": ...})` continues the clarification node, records the
  answers, changes status to `pending`, and routes back to requirements.
- `Command(resume={"reject": true})` changes status to `rejected` and routes to `END`.
- `failed` for empty input, missing configuration, prompt loading errors, invalid
  or incomplete output, model refusals, or API errors. Requirements are cleared
  and a safe message is stored in `state["error"]`.

## Design Agent

Opt in to execute through design:

```bash
python run.py start --through design --requirement "Build a URL shortener with create and redirect endpoints using FastAPI and SQLite"
```

The existing shorthand also works:

```bash
python run.py --requirement "Build a URL shortener service" --through design
```

`--through` defaults to `requirements`, preserving Stage 1 and Stage 2 behavior.
It is stored in the checkpoint. If a design-enabled run pauses, answer it with
the usual resume command; no need to re-enter requirements or repeat the option:

```bash
python run.py start --through design --requirement "Build a secure and scalable web application"
python run.py resume --run-id <returned_run_id> --answers "Users create short URLs and follow them to the original URLs."
```

Selecting `--through design` authorizes technical design once requirements are
complete; this MVP has no separate requirements approval interrupt. Blocking
clarification still pauses execution. Rejection and requirements failures never
run design. The Design Agent independently validates structured requirements and
refuses inputs flagged as requiring clarification.

The agent reads `prompts/design.md`, uses the same `.env` and OpenAI structured
output integration, and receives the structured requirements, original request,
and clarification history from state. It respects explicit technology constraints.
FastAPI, SQLite, and pytest are the URL shortener defaults when unspecified;
inferred choices are documented as assumptions, with tradeoffs recorded as risks.

`models/design.py` defines the `DesignOutput` schema: architecture summary,
technology stack, components, API endpoints (method/path/request/response/errors),
data models, non-functional design, ordered implementation plan, assumptions,
risks, and story traceability. Traceability must contain every story exactly once
and one coverage explanation per acceptance criterion. Structural validation
does not establish technical correctness; review the design before implementation.

Clear requirements route `requirements -> design -> END`, ending with
`design_complete`. Ambiguous ones route through `pause -> clarification`, then
return to `requirements` after answers, and only then to design if clear. Design
errors end with `failed`, preserve the structured requirements, and clear design.

Design-enabled completed runs additionally save `design.json` in the existing
`workspace/runs/<run_id>/` directory. The CLI report and `status.json` also include
`design` and `through`. A paused or rejected run has no design artifact. SQLite
stores both the stage choice and design state with the original thread ID.
Completed requirements-only runs cannot be upgraded using `resume`; start a new
design-enabled run instead. Runs paused before Stage 3 remain requirements-only.

## Test

```bash
python -m pytest -q
```

Tests mock the LLM and disable local `.env` loading: no API key or network is
needed. Coverage includes structured output validation, clarification, failures,
prompt loading, conditional graph transitions, CLI JSON, rejection, repeated
clarification, and SQLite checkpoint persistence across processes. Design tests
cover schema and traceability validation, successful generation, missing or
ineligible requirements, LLM errors, and clarification resume through design.

Requirements and Design Agents are implemented. The architecture diagram illustrates
the broader planned workflow; no Developer Agent or application code generation is implemented.
