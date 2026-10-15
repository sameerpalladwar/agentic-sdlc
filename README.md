# Agentic SDLC Harness

LangGraph harness for an agentic software development lifecycle workflow.
Stage 1 converts natural-language requirements into structured user stories using OpenAI.

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
python run.py --requirement "Build a URL shortener service"
```

The CLI prints readable requirements JSON to stdout and saves the same object to
`workspace/runs/<run_id>/requirements.json`. Each run gets a unique ID. Status and
the saved path go to stderr. Run artifacts are ignored by Git. Clarification
results are also saved; review their open questions before continuing manually.
Failures print a safe error to stderr, exit with code 1, and do not save a result.
Missing CLI arguments exit with code 2. No generated commands are executed.

## Requirements Agent

Instructions live in `prompts/requirements.md` and are loaded relative to the
repository, independently of the working directory. Pydantic models in
`models/requirements.py` validate the OpenAI SDK structured output.

The result includes `summary`, `user_stories`, `non_functional_requirements`,
`assumptions`, `open_questions`, and `requires_human_clarification`. Each story
has an ID, title, `as_a`, `i_want`, `so_that`, MoSCoW priority (`must`, `should`,
`could`, `wont`), and acceptance criteria with `given`, `when`, and `then` fields.
Blocking ambiguity produces targeted questions rather than invented requirements.

The graph remains `START -> requirements -> END`. It starts with `pending` and
stores validated data in `state["requirements"]`. The requirements node returns:

- `requirements_complete` for usable requirements.
- `needs_human_clarification` for blocking ambiguity; this ends the run for human review.
- `failed` for empty input, missing configuration, prompt loading errors, invalid
  or incomplete output, model refusals, or API errors. Requirements are cleared
  and a safe message is stored in `state["error"]`.

## Test

```bash
python -m pytest -q
```

Tests mock the LLM and disable local `.env` loading: no API key or network is
needed. Coverage includes structured output validation, clarification, failures,
prompt loading, graph transitions, CLI JSON, and per-run persistence.

Only the Requirements Agent is implemented. The architecture diagram illustrates
the broader planned workflow; other SDLC agents and approval gates remain future work.
