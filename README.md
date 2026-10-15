# Agentic SDLC Harness

Minimal LangGraph skeleton for an agentic software development lifecycle workflow.

## Setup

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## Run

```bash
python run.py
```

## Test

```bash
python -m pytest -q
```

This first iteration contains one deterministic requirements node. LLM integration, review, QA, approval gates, and persistence will be added incrementally.
