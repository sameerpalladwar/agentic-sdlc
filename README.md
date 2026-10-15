# Agentic SDLC Harness

LangGraph harness for an agentic software development lifecycle workflow.
Stage 1 converts natural-language requirements into structured user stories using OpenAI.
Stage 2 pauses ambiguous runs for human clarification and resumes from persistent checkpoints.

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

- `requirements_complete` goes directly to `END` for usable requirements.
- `needs_human_clarification` goes through a pause node, which persists `paused`,
  and then a clarification node calls LangGraph `interrupt()` with the open questions.
- `Command(resume={"answers": ...})` continues the clarification node, records the
  answers, changes status to `pending`, and routes back to requirements.
- `Command(resume={"reject": true})` changes status to `rejected` and routes to `END`.
- `failed` for empty input, missing configuration, prompt loading errors, invalid
  or incomplete output, model refusals, or API errors. Requirements are cleared
  and a safe message is stored in `state["error"]`.

## Test

```bash
python -m pytest -q
```

Tests mock the LLM and disable local `.env` loading: no API key or network is
needed. Coverage includes structured output validation, clarification, failures,
prompt loading, conditional graph transitions, CLI JSON, rejection, repeated
clarification, and SQLite checkpoint persistence across processes.

Only the Requirements Agent is implemented. The architecture diagram illustrates
the broader planned workflow; other SDLC agents remain future work.
