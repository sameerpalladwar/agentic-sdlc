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
