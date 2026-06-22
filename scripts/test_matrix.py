from __future__ import annotations

import argparse
import importlib.util
import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LANE_PATHS = {
    "unit": ("tests/unit",),
    "runtime": ("tests/runtime",),
    "security": ("tests/security",),
    "self-evolution": ("tests/self_evolution",),
    "providers": ("tests/providers",),
    "benchmark": ("tests/benchmarks",),
}
PYTHON_LANES = tuple(LANE_PATHS)
DEFAULT_MAX_LANE_SECONDS = 300.0
DEFAULT_WORKERS = "1"
XDIST_DISTS = ("loadfile", "loadscope", "load", "worksteal")


@dataclass(frozen=True)
class Command:
    name: str
    argv: list[str]
    env: dict[str, str] | None = None
    enforce_duration: bool = True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run Agent-libOS test lanes.")
    parser.add_argument(
        "--lane",
        choices=[*PYTHON_LANES, "gui", "all"],
        required=True,
        help="test lane to run",
    )
    parser.add_argument("--run-real-deno", action="store_true", help="include tests marked real_deno")
    parser.add_argument("--run-real-llm", action="store_true", help="include tests marked real_llm")
    parser.add_argument(
        "--max-lane-seconds",
        type=float,
        default=DEFAULT_MAX_LANE_SECONDS,
        help="duration budget for individual lanes; ignored by --lane all",
    )
    parser.add_argument(
        "-n",
        "--workers",
        type=_worker_count,
        default=DEFAULT_WORKERS,
        help="number of pytest-xdist workers for Python lanes; use 1 to run serially, or auto/logical",
    )
    parser.add_argument(
        "--dist",
        choices=XDIST_DISTS,
        default="loadfile",
        help="pytest-xdist scheduling strategy used when --workers is greater than 1",
    )
    args = parser.parse_args(argv)
    _validate_args(parser, args)

    commands = _commands_for(args)
    for command in commands:
        status = _run(command, max_seconds=args.max_lane_seconds)
        if status != 0:
            return status
    return 0


def _commands_for(args: argparse.Namespace) -> list[Command]:
    if args.lane == "gui":
        npm = _required_tool("npm")
        return [
            Command("gui unit tests", [npm, "--prefix", "gui", "run", "test"]),
            Command("gui typecheck", [npm, "--prefix", "gui", "run", "typecheck"]),
            Command("gui build", [npm, "--prefix", "gui", "run", "build"]),
        ]
    if args.lane == "all":
        return [
            Command(
                f"pytest all deterministic lanes{_worker_suffix(args)}",
                _pytest_args(("tests",), args),
                env=_pytest_env(args),
                enforce_duration=False,
            )
        ]
    return [
        Command(
            f"pytest {args.lane}{_worker_suffix(args)}",
            _pytest_args(LANE_PATHS[args.lane], args),
            env=_pytest_env(args),
        )
    ]


def _pytest_args(paths: tuple[str, ...], args: argparse.Namespace) -> list[str]:
    command = [sys.executable, "-m", "pytest", *paths]
    if _workers_enabled(args):
        command.extend(["-n", args.workers, "--dist", args.dist])
    marker_filters: list[str] = []
    if args.run_real_deno:
        command.append("--run-real-deno")
    else:
        marker_filters.append("not real_deno")
    if args.run_real_llm:
        command.append("--run-real-llm")
    else:
        marker_filters.append("not real_llm")
    if marker_filters:
        command.extend(["-m", " and ".join(marker_filters)])
    return command


def _validate_args(parser: argparse.ArgumentParser, args: argparse.Namespace) -> None:
    if args.lane == "gui" and _workers_enabled(args):
        parser.error("--workers only applies to pytest lanes; run the gui lane separately")
    if _workers_enabled(args) and importlib.util.find_spec("xdist") is None:
        parser.error("pytest-xdist is required for --workers; run `uv sync --all-groups` first")


def _worker_count(value: str) -> str:
    text = str(value).strip().lower()
    if text in {"auto", "logical"}:
        return text
    try:
        count = int(text)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("workers must be a positive integer, auto, or logical") from exc
    if count < 1:
        raise argparse.ArgumentTypeError("workers must be >= 1")
    return str(count)


def _workers_enabled(args: argparse.Namespace) -> bool:
    return str(args.workers) != DEFAULT_WORKERS


def _worker_suffix(args: argparse.Namespace) -> str:
    if not _workers_enabled(args):
        return ""
    return f" ({args.workers} workers, dist={args.dist})"


def _pytest_env(args: argparse.Namespace) -> dict[str, str] | None:
    if args.run_real_llm:
        return {"AGENT_LIBOS_RUN_REAL_LLM_BENCHMARK": "1"}
    return None


def _run(command: Command, *, max_seconds: float) -> int:
    print(f"==> {command.name}", flush=True)
    env = os.environ.copy()
    if command.env:
        env.update(command.env)
    started = time.perf_counter()
    result = subprocess.run(command.argv, cwd=ROOT, env=env)
    elapsed = time.perf_counter() - started
    print(f"==> {command.name} finished in {elapsed:.2f}s", flush=True)
    if result.returncode != 0:
        return result.returncode
    if command.enforce_duration and elapsed > max_seconds:
        print(
            f"{command.name} exceeded lane budget: {elapsed:.2f}s > {max_seconds:.2f}s",
            file=sys.stderr,
        )
        return 2
    return 0


def _required_tool(name: str) -> str:
    resolved = shutil.which(name)
    if resolved is None:
        raise SystemExit(f"required tool is not on PATH: {name}")
    return resolved


if __name__ == "__main__":
    raise SystemExit(main())
