# Agent libOS

This repository contains a runnable Python MVP for the design in
`agent_libos_design_doc.md`.

The implementation focuses on the document's MVP path:

- Agent process lifecycle: spawn, fork, exec, wait, signal, pause, resume, exit.
- Typed object memory with object handles, links, views, materialization, and merge.
- Capability checks for object access and tool execution.
- Human approval and interrupt primitives.
- Ephemeral Python JIT tools validated and executed through a sandbox abstraction.
- SQLite-backed event, audit, process, memory, human request, tool, and checkpoint state.
- A small CLI and integration tests that exercise the coding-agent demo flow.

It intentionally does not claim production-grade sandbox isolation, distributed
scheduling, multi-tenant policy, or a persistent global skill marketplace. Those
remain extension points matching the design document.

## Quick Start

Create the project environment from the lockfile:

```bash
uv sync
```

Run tests and the demo through uv:

```bash
uv run python -m unittest discover -s tests
uv run agent-libos demo
```

You can also create a local runtime database:

```bash
uv run agent-libos init --db .agent_libos.sqlite
uv run agent-libos demo --db .agent_libos.sqlite
uv run agent-libos audit --db .agent_libos.sqlite
```

## Dependency Management

This project is managed with uv. Add runtime dependencies with
`uv add <package>` and development dependencies with `uv add --dev <package>`.
Commit both `pyproject.toml` and `uv.lock` after dependency changes.
