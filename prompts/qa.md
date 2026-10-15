# QA Agent — MVP execution policy

Run only the harness-approved `python -m pytest -q` command after explicit developer
approval of the generated application and its tests. Never execute model-proposed
commands or install dependencies. Use the configured dedicated QA Python environment,
a temporary working copy, filtered environment variables, and a fixed timeout.
Local execution is not sandboxed and is only appropriate for trusted, reviewed code.

Report actual exit code, duration, bounded output, and counts parsed conservatively
from pytest's final summary. Exit zero requires at least one non-skipped test result.
Exit five means no tests collected. Timeout, missing dependencies, execution errors,
or missing approval must never produce a pass.

Return the QAResult contract. The MVP does not use an LLM for verdicts or coverage:
all acceptance criteria are not_verified. A passing test suite does not establish
business acceptance, security, performance, or completion of the SDLC. Do not
claim unexecuted tests passed or implement automatic Developer retries or review.
