from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path


SCRIPT_DIR = Path(__file__).resolve().parent
DISCOVER_SCRIPT = SCRIPT_DIR / "discover_doc_assets_from_entries.py"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run discover_doc_assets_from_entries.py in repeated small batches."
    )
    parser.add_argument("--batch-size", type=int, default=50, help="Entries per batch.")
    parser.add_argument(
        "--max-batches", type=int, default=10, help="Maximum number of batches to run."
    )
    parser.add_argument(
        "--timeout-seconds",
        type=int,
        default=8,
        help="Per-entry probe timeout in seconds.",
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="Preview only the first batch."
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    completed = 0

    for batch_no in range(1, args.max_batches + 1):
        command = [
            sys.executable,
            str(DISCOVER_SCRIPT),
            "--limit",
            str(args.batch_size),
            "--timeout-seconds",
            str(args.timeout_seconds),
        ]
        if args.dry_run:
            command.append("--dry-run")

        result = subprocess.run(
            command,
            cwd=str(SCRIPT_DIR.parent),
            capture_output=True,
            text=True,
        )

        stdout_text = result.stdout.strip() if result.stdout else ""
        stderr_text = result.stderr.strip() if result.stderr else ""
        payload = None
        if stdout_text:
            try:
                payload = json.loads(stdout_text)
            except json.JSONDecodeError:
                payload = {"raw_stdout": stdout_text}

        if result.returncode != 0:
            print(
                json.dumps(
                    {
                        "status": "failed",
                        "batch_no": batch_no,
                        "stdout": stdout_text,
                        "stderr": stderr_text,
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
            return result.returncode

        print(
            json.dumps(
                {
                    "status": "batch_completed",
                    "batch_no": batch_no,
                    "payload": payload,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        completed += 1

        if args.dry_run:
            break
        if (
            payload
            and payload.get("discovered_count") == 0
            and payload.get("failed_probes") == 0
        ):
            break

    print(
        json.dumps(
            {
                "status": "done",
                "completed_batches": completed,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
