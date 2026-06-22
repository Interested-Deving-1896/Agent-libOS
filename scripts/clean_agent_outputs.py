from __future__ import annotations

import argparse
import json
from pathlib import Path

from scripts.agent_outputs import cleanup_agent_outputs


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Clean ignored Agent libOS workspace outputs.")
    parser.add_argument("--root", default="agent_outputs", help="output directory to clean")
    parser.add_argument("--yes", action="store_true", help="delete files; default is a dry run")
    parser.add_argument("--limit", type=int, default=100, help="maximum paths to print")
    args = parser.parse_args(argv)

    root = Path(args.root)
    dry_run = not args.yes
    paths = cleanup_agent_outputs(root, baseline=set(), dry_run=dry_run)
    limit = max(0, args.limit)
    print(
        json.dumps(
            {
                "root": str(root),
                "dry_run": dry_run,
                "count": len(paths),
                "paths": paths[:limit],
                "truncated": len(paths) > limit,
            },
            indent=2,
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
