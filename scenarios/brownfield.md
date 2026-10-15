# Brownfield acceptance examples

```bash
python run.py start --requirement "Add URL expiration to my existing URL shortener"
python run.py resume --run-id <run_id> --repo-path ./existing-app
```

Expected: pause for the missing repository without an LLM call; resume the same run as
Brownfield. Invalid paths remain correctable using the same resume command.

```bash
python run.py start --repo-path ./existing-app --requirement "Add URL expiration" --stop-after delivery
```

Expected: validate and analyze the repository before Requirements. Source is read-only;
development modifies a managed copy under `workspace/output_code/<run_id>/`.
The small `examples/brownfield/` Python library is an optional repository fixture; pass its
path explicitly to enhance it. Never substitute it for a missing user repository.
This file is documentation, not runtime configuration.
