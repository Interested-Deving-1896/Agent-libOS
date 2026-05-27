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
uv run agent-libos --db .agent_libos.sqlite init
uv run agent-libos --db .agent_libos.sqlite demo
uv run agent-libos --db .agent_libos.sqlite audit
```

## LLM Execution

Runnable processes can be executed by an OpenAI-compatible chat completion
endpoint. Keep credentials in a local `.env` file:

```bash
OPENAI_CODING_AGENT_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
OPENAI_LANGUAGE_MODEL=qwen3.7-max
OPENAI_API_KEY=...
```

Then spawn and run processes through uv:

```bash
uv run agent-libos --db .agent_libos.sqlite spawn --image coding-agent:v0 --goal "Analyze the pytest failure log"
uv run agent-libos --db .agent_libos.sqlite grant-tool <pid> write_text_file
uv run agent-libos --db .agent_libos.sqlite run --max-quanta 5
```

For a one-shot document summary demo, spawn an Agent process that reads a
workspace document and speaks a one-sentence overview to the terminal:

```bash
uv run python scripts/llm_summarize_document.py agent_libos_design_doc.md --trace
```

Each LLM quantum materializes the process MemoryView, sends it with the
AgentImage system prompt, exposes registered Skills/Tools Layer tools as OpenAI
tool schemas, executes the last valid tool call, and dispatches it through the
same capability, human approval, tool broker, checkpoint, and audit path as the
SDK.
Free-form assistant text is allowed; if a provider cannot emit tool calls, the
runtime falls back to parsing the final JSON action object in the text.

## Dependency Management

This project is managed with uv. Add runtime dependencies with
`uv add <package>` and development dependencies with `uv add --dev <package>`.
Commit both `pyproject.toml` and `uv.lock` after dependency changes.
