from __future__ import annotations

import argparse
import json
from pathlib import Path

from benchmarks.runtime_publication_recovery import (
    PUBLICATION_SCALE_PROFILES,
    run_publication_scale_benchmark,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run the deterministic runtime-publication reopen scale benchmark."
    )
    parser.add_argument(
        "--profile",
        choices=sorted(PUBLICATION_SCALE_PROFILES),
        default="ci",
        help="ci seeds 10k terminal publications with a 1001-row repair backlog.",
    )
    parser.add_argument("--total-records", type=int)
    parser.add_argument("--unreconciled-records", type=int)
    parser.add_argument("--page-size", type=int)
    parser.add_argument(
        "--output",
        type=Path,
        default=Path(".benchmark_runs/publication-reconciliation-scale.json"),
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    profile = PUBLICATION_SCALE_PROFILES[args.profile]
    result = run_publication_scale_benchmark(
        total_records=(
            args.total_records
            if args.total_records is not None
            else profile.total_records
        ),
        unreconciled_records=(
            args.unreconciled_records
            if args.unreconciled_records is not None
            else profile.unreconciled_records
        ),
        page_size=args.page_size if args.page_size is not None else profile.page_size,
    )
    payload = result.as_dict()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(payload, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
