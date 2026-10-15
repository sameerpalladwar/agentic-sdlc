# Clarification acceptance examples

```bash
python run.py start --requirement "URL links for our team"
python run.py resume --run-id <run_id> --answers "Build a new application"
```

Expected: uncertain project intent pauses instead of guessing Greenfield.

```bash
python run.py start --requirement "Build a URL management system for our team"
```

Expected: new-application intent selects Greenfield; the existing Requirements Agent asks
blocking functional clarification questions when needed. The same clarification gate is
available after Brownfield analysis. Resume with `--answers` or cancel with `--reject`.
No questions or approval gates are answered automatically.
This file is documentation, not runtime configuration.
