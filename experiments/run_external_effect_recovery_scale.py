from __future__ import annotations

import argparse
import json
from pathlib import Path

from benchmarks.external_effect_recovery import (
    BENCHMARK_PROFILES,
    run_recovery_scale_benchmark,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run the deterministic external-effect recovery scale benchmark."
    )
    parser.add_argument(
        "--profile",
        choices=sorted(BENCHMARK_PROFILES),
        default="ci",
        help="ci seeds 100k records; million seeds 1m records.",
    )
    parser.add_argument("--total-records", type=int)
    parser.add_argument("--pending-records", type=int)
    parser.add_argument("--page-size", type=int)
    parser.add_argument(
        "--output",
        type=Path,
        default=Path(".benchmark_runs/external-effect-recovery-scale.json"),
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    profile = BENCHMARK_PROFILES[args.profile]
    result = run_recovery_scale_benchmark(
        total_records=(
            args.total_records
            if args.total_records is not None
            else profile.total_records
        ),
        pending_records=(
            args.pending_records
            if args.pending_records is not None
            else profile.pending_records
        ),
        page_size=(
            args.page_size if args.page_size is not None else profile.page_size
        ),
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
