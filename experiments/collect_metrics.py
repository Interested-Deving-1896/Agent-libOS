from __future__ import annotations

import argparse
import json

from agent_libos.utils.serde import to_jsonable
from benchmarks.runtime_safety.metrics import write_metrics


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Collect metrics from a runtime-safety benchmark run directory.")
    parser.add_argument("run_dir")
    args = parser.parse_args(argv)
    metrics = write_metrics(args.run_dir)
    print(json.dumps(to_jsonable(metrics), indent=2, ensure_ascii=False))
    return 0 if metrics.get("valid", False) else 2


if __name__ == "__main__":
    raise SystemExit(main())
