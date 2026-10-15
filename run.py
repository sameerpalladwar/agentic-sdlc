import argparse
import json
import sys
from pathlib import Path
from uuid import uuid4

from orchestrator.graph import build_graph


RUNS_DIR = Path(__file__).resolve().parent / "workspace" / "runs"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Convert a software requirement into user stories.")
    parser.add_argument("--requirement", required=True, help="Natural-language software requirement")
    args = parser.parse_args(argv)
    result = build_graph().invoke({"requirement": args.requirement, "status": "pending"})
    if result["status"] == "failed":
        print(json.dumps({"status": "failed", "error": result["error"]}, indent=2), file=sys.stderr)
        return 1

    output = json.dumps(result["requirements"], indent=2, ensure_ascii=False)
    run_id = uuid4().hex
    try:
        run_dir = RUNS_DIR / run_id
        run_dir.mkdir(parents=True, exist_ok=False)
        (run_dir / "requirements.json").write_text(output + "\n", encoding="utf-8")
    except OSError:
        print("Unable to save requirements in workspace/runs.", file=sys.stderr)
        return 1
    print(output)
    print(f"Status: {result['status']}\nSaved: {run_dir / 'requirements.json'}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
