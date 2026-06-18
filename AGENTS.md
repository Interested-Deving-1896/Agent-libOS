# Repository Guidelines

## Project Structure & Module Organization

Agent libOS is a Python runtime with an optional Electron GUI. Core runtime code
lives in `agent_libos/`, organized by subsystem: `runtime/`, `primitives/`,
`capability/`, `memory/`, `skills/`, `modules/`, `tools/`, `substrate/`, and
`api/` for CLI/GUI server entrypoints. Tests live in `tests/` and follow the
same feature boundaries. Benchmark code and fixtures are under
`benchmarks/runtime_safety/`, with runners in `experiments/`. Documentation is
in `docs/`; the Electron/React frontend is in `gui/`; example skills live in
`skills/`.

## Build, Test, and Development Commands

- `uv sync --frozen`: install the locked Python environment.
- `uv run python -m compileall agent_libos tests scripts experiments benchmarks`:
  catch syntax/import errors.
- `uv run python -m unittest discover -s tests -v`: run the Python test suite.
- `uv run agent-libos --help`: inspect CLI commands.
- `uv run python experiments/run_benchmark.py --suite benchmarks/runtime_safety --runner agent_libos_full --limit 3 --output .benchmark_runs/smoke`: run a deterministic benchmark smoke.
- `npm --prefix gui run typecheck` and `npm --prefix gui run build`: validate
  the Electron/React app.

## Coding Style & Naming Conventions

Use Python 3.11+ with 4-space indentation, type hints for public interfaces, and
dataclasses or Pydantic models for structured data. Keep runtime defaults in
`agent_libos.config.DEFAULT_CONFIG`; do not scatter magic numbers. Preserve the
core boundary: tools and Skills affect visibility, while primitives enforce
Capability v2, human approval, provider policy, events, and audit. TypeScript in
`gui/` should use strict component and API types.

## Testing Guidelines

Add or update tests with each behavior change. Name Python tests
`tests/test_<feature>.py` and test methods `test_<expected_behavior>`. Security
or authority changes need denial-path tests, audit/event assertions, and, where
relevant, benchmark coverage. Real LLM and Deno paths must remain opt-in; default
tests should be deterministic and token-free.

## Commit & Pull Request Guidelines

Recent commits are short topic summaries such as `GUI` or
`checkpoint commit to image`; prefer concise imperative subjects with a clear
scope, for example `harden checkpoint fork authority`. PRs should describe the
runtime invariant affected, list tests run, link issues or design notes, and
include GUI screenshots for visible frontend changes.

## Security & Configuration Tips

Never commit `.env`, credentials, benchmark outputs, or GUI build artifacts.
Remote access must go through registered JSON-RPC endpoints, not model-supplied
URLs. Checkpoint restore and image commit do not roll back or package external
provider state; provider-classified effects remain append-only audit records.
