from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path


SCRIPT_DIR = Path(__file__).resolve().parent
REFRESH_SCRIPT = SCRIPT_DIR / "refresh_core_assets_from_cmc.py"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run refresh_core_assets_from_cmc.py in repeated batches."
    )
    parser.add_argument("--batch-size", type=int, default=500, help="Rows per batch.")
    parser.add_argument(
        "--max-batches", type=int, default=10, help="Maximum number of batches to run."
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
            str(REFRESH_SCRIPT),
            "--limit",
            str(args.batch_size),
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

        processed_rows = payload.get("processed_rows") if payload else None
        if args.dry_run:
            break
        if processed_rows == 0:
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
