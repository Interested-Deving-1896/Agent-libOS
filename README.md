# Agent libOS

An experimental Agent-native libOS runtime written in Python.

Agent libOS models an agent as a long-running, schedulable, interruptible, capability-controlled `AgentProcess`, not as a single chat request or workflow thread. The current codebase is an MVP implementation of [agent_libos_design_doc.md](agent_libos_design_doc.md).

Working in progress.

## Features & TODOs

Legend:

- `[x]` implemented in the current MVP.
- `[~]` implemented partially or with a local-only prototype.
- `[ ]` planned.

### Core Runtime

- [x] Agent process lifecycle: `spawn`, `fork`, `exec`, `wait`, `signal`, `pause`, `resume`, `exit`.
- [x] Simple runnable-process scheduler with one LLM quantum per process turn.
- [x] Agent images with default tools, context policy, safety profile, and required capabilities.
- [x] Typed object memory with object handles, links, views, materialization, snapshots, and merge.
- [x] Object Memory objects have globally unique names; authorized processes can resolve objects by name without treating names as capabilities.
- [x] Object payloads live in runtime memory, not SQLite; SQLite stores only object directory metadata, and process-owned memory is released on process exit unless retained as the process result.
- [x] Capability manager for object access, tool execution, external resources, and revocation.
- [x] Event bus and audit trace for process, memory, capability, tool, human, checkpoint, and external access events.
- [x] SQLite-backed runtime store for processes, objects, links, capabilities, events, audit records, human requests, tools, candidates, and checkpoints.
- [~] Checkpoint and rollback for runtime state.
- [ ] Distributed scheduling and durable multi-worker process execution.
- [ ] Quotas for CPU, wall time, memory, token budget, child processes, and external side effects.

### Skills / Tools Layer

- [x] `BaseAgentTool` model with Pydantic input/output schemas, policy metadata, timeout handling, and OpenAI-compatible tool schema generation.
- [x] ToolBroker registration, process tool-table visibility checks, result object creation, event emission, and audit logging.
- [x] Built-in tools:
  - `create_memory_object`
  - `process_exit`
  - `request_permission`
  - `create_object_from_file`
  - `write_object_to_file`
  - `read_text_file`
  - `write_text_file`
  - `human_output`
  - `parse_pytest_log`
  - `echo`
- [x] LLM-facing tools are wrappers over libOS primitives, not the security boundary themselves.
- [x] `read_text_file` and `write_text_file` now call the libOS filesystem primitive instead of touching the host filesystem directly.
- [x] `human_output` now calls the HumanObject output primitive instead of writing to the terminal directly.
- [x] `request_permission` wraps the human permission-request primitive so an Agent can ask for `always_allow`, `always_deny`, or `ask_each_time` policy on a capability resource.
- [~] Ephemeral Python JIT tools with sandboxed validation and registration.
- [~] Skills/tools registries and bundles as local scaffolding.
- [ ] Persistent signed tool registry.
- [ ] Production sandbox profiles for JIT tools and high-risk tools.
- [ ] Rich tool policy engine for confirmation, checkpointing, retry, compensation, and capability attenuation.
- [ ] MCP adapter and richer tool transport formats.

### LLM Execution

- [x] OpenAI-compatible chat completions client using `.env` configuration.
- [x] OpenAI tool calls generated from registered Skills/Tools Layer tools.
- [x] Free-form model text is allowed; the runtime executes the last legal tool call.
- [x] Fallback JSON action parser for providers that cannot emit tool calls.
- [x] System prompt aligned with the libOS model: tool calls are libc-like wrappers over libOS primitives, not syscalls.
- [x] Real-model smoke scripts for file-writing and document-summary goals.
- [ ] Streaming model output.
- [ ] Multi-turn tool result compaction and long-context paging.
- [ ] Model/provider conformance test suite.

### External Objects and Human Objects

- [x] Filesystem adapter as a libOS external-object primitive with workspace containment, capability checks, events, and audit records.
- [x] HumanObject manager with queued query, approve, reject, interrupt, and output primitives.
- [x] Human terminal queue for processing pending human-facing messages in order.
- [x] Permission requests are handled through the human queue; `ask_each_time` causes the filesystem write primitive to create a per-use human approval request.
- [~] Shell/browser/git/database external adapters are placeholders or local stubs.
- [ ] ExternalRef objects with snapshots and provenance.
- [ ] Browser, git, database, mail, calendar, search, and API-service adapters.
- [ ] Human role/authority profiles.
- [ ] Interrupt delivery policies and human availability model.

### Security

- [x] Object handles are capability-protected; OIDs alone do not grant access.
- [x] Process-visible tools are fixed by the AgentImage/tool table at process creation time.
- [x] Tool calls in a process tool table are allowed; security checks happen at libOS primitives.
- [x] External filesystem read/write requires filesystem capability at the primitive layer.
- [x] Filesystem write permission supports `always_allow`, `always_deny`, and `ask_each_time`; per-use approval grants are consumed after one successful primitive call.
- [x] Human output requires `human:owner` write capability at the primitive layer.
- [x] External access is audited at the libOS primitive boundary.
- [x] Boundary tests verify that tools cannot bypass filesystem or human capability checks, path containment, revoked capabilities, fork attenuation, or JIT process scope.
- [~] JIT tool sandbox blocks selected dangerous imports and executes candidate code out of process.
- [ ] Strong isolation for filesystem, network, environment variables, CPU, memory, and wall time.
- [ ] Multi-tenant policy engine.
- [ ] Secret redaction and credential access policy.
- [ ] Formal side effect compensation model.

### CLI, Scripts, and Tests

- [x] `agent-libos` CLI for init, demo, audit, process listing, tool listing, spawn, LLM run, and terminal human queue processing.
- [x] Demo flow covering process, memory, worker fork, JIT parser, checkpoint, human approval, tool call, filesystem capability denial before grant, write result, final report, and audit trace.
- [x] `scripts/llm_summarize_document.py`: start an Agent process that reads a workspace document, requests write permission, writes a one-sentence summary when allowed, and reports the output filename or denial reason.
- [x] `scripts/llm_write_goal_smoke.py`: real-model smoke test for writing a workspace file.
- [x] Unit tests for external safety boundaries and the demo contract.
- [ ] Broader regression tests for process/memory/checkpoint/JIT/LLM behavior.
- [ ] CI workflow.
- [ ] API reference documentation.

## Architecture

```text
Agent Personality / Application
  -> Skills / Tools Layer
     - LLM-facing actions
     - tool schemas
     - macro actions
     - skill metadata
  -> Agent libOS Runtime
     - ProcessManager
     - ObjectMemoryManager
     - ToolBroker
     - HumanObjectManager
     - ExternalObjectAdapters
     - CapabilityManager
     - EventBus
     - CheckpointManager
     - AuditManager
  -> Host Runtime
     - OpenAI-compatible model API
     - SQLite
     - local workspace filesystem
     - subprocess sandbox
     - terminal human sink
```

The important boundary is between LLM-facing tools and libOS primitives. A tool is a stable model-facing wrapper, similar to a libc function. The actual security checks and host interaction live in libOS primitives such as `FilesystemAdapter.read_text`, `FilesystemAdapter.write_text`, and `HumanObjectManager.output`.

For example, `write_text_file` is callable only if it is present in the process tool table, which is derived from the AgentImage when the process is created. The tool call itself is allowed once visible to the process, but the filesystem primitive still requires:

- write capability on the target filesystem resource.

Putting a tool in the process table does not grant access to files, humans, shell, network, secrets, or other host resources.

## Quick Start

### 1. Install Dependencies

This project is managed with uv:

```bash
uv sync
```

### 2. Run Tests

```bash
uv run python -m unittest discover -s tests -v
```

### 3. Run the Local Demo

```bash
uv run agent-libos demo
```

The demo is deterministic and does not call a real model. It analyzes a synthetic pytest failure, forks a worker, validates and calls a JIT parser, checkpoints before writing, verifies that filesystem write is denied before the external-resource capability is granted, requests human approval for workspace write capability, writes `agent_outputs/demo_patch_preview.txt`, and returns a final report object with the tool sequence, authorization records, external side effect, and audit summary.

You can also create and inspect a persistent local runtime database:

```bash
uv run agent-libos --db .agent_libos.sqlite init
uv run agent-libos --db .agent_libos.sqlite demo
uv run agent-libos --db .agent_libos.sqlite audit
uv run agent-libos --db .agent_libos.sqlite processes
uv run agent-libos --db .agent_libos.sqlite tools
uv run agent-libos --db .agent_libos.sqlite human
```

## LLM Execution

Runnable processes can be executed by an OpenAI-compatible chat completion endpoint. Keep credentials in a local `.env` file:

```bash
OPENAI_CODING_AGENT_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
OPENAI_LANGUAGE_MODEL=qwen3.7-max
OPENAI_API_KEY=...
```

Spawn a process and run the LLM scheduler:

```bash
uv run agent-libos --db .agent_libos.sqlite spawn --image coding-agent:v0 --goal "Analyze the pytest failure log"
uv run agent-libos --db .agent_libos.sqlite run --max-quanta 5
```

The process tool list is fixed by the AgentImage at creation time. High-risk external effects still need external-resource capability at the libOS primitive layer.
Processes can use `request_permission` to ask the human for a permission policy. If the human chooses `ask_each_time`, the relevant primitive asks again for each use and only receives a one-shot grant when that specific operation is approved.

## Example Scripts

Summarize a workspace document through an Agent process:

```bash
uv run python scripts/llm_summarize_document.py agent_libos_design_doc.md --trace
```

For non-interactive runs, choose the human permission-policy response explicitly:

```bash
uv run python scripts/llm_summarize_document.py README.md --permission-policy always_allow --auto-approve --trace
```

Run the real-model write-file smoke test:

```bash
uv run python scripts/llm_write_goal_smoke.py
```

The smoke test uses the `coding-agent:v0` process tool table, which includes `write_text_file`, and explicitly grants workspace filesystem write capability.

Copy a workspace text file through named Object Memory without materializing the file content into the process prompt:

```bash
uv run python scripts/object_memory_file_copy_smoke.py --trace
```

## How to Write Agent libOS Tools

Tools live in the Skills / Tools Layer and should not directly access host resources.

Use this pattern:

1. Define a Pydantic input schema and optional output schema.
2. Subclass `SyncAgentTool` or `BaseAgentTool`.
3. Keep validation and model-facing ergonomics in the tool.
4. Call `ctx.runtime.<primitive>` for process, memory, filesystem, human, or other libOS operations.
5. Let libOS primitives enforce capability checks, containment, audit, event emission, checkpointing, and future policy hooks.
6. Register the tool through `Runtime._register_builtin_tools()` or a ToolBroker-backed registry.

Do not put direct filesystem, terminal, network, shell, browser, database, or credential access inside a tool implementation unless that code is itself the libOS primitive or a sandbox backend.

## Module Map

```text
agent_libos/
  api/             CLI entry points and demo orchestration
  capability/      Capability grant, revoke, check, and object handles
  external/        External-object adapters such as filesystem and shell
  human/           HumanObject query, approval, interrupt, and output primitives
  images/          Built-in AgentImage definitions
  llm/             Prompt, context, OpenAI-compatible client, executor, action parser
  memory/          Typed Object Memory and MemoryView implementation
  runtime/         Runtime composition, process manager, scheduler, events, checkpoints, audit
  skills/          Skill schema, registry, verifier, linker scaffolding
  skills_tools/    Tool/action registry and bundle scaffolding
  storage/         SQLite persistence
  tools/           Tool base classes, ToolBroker, sandbox, and built-in tools
scripts/           Real-model smoke and demo scripts
tests/             Safety-boundary and regression tests
```

## Roadmap

Near-term priorities:

- Expand tests for process lifecycle, memory view semantics, checkpoint rollback, JIT registration, and LLM executor behavior.
- Move remaining external-object placeholders behind capability-aware primitives.
- Introduce explicit policy decisions for external side effects: allow, deny, require human approval, require checkpoint, or require sandbox.
- Add a production-grade sandbox boundary for JIT tools.
- Add ExternalRef objects and snapshots for external resources entering Object Memory.
- Add CI and API documentation.

Longer-term directions:

- Persistent signed skill/tool registry.
- Distributed process scheduler.
- Rich human role and authority model.
- Tool result compaction and context paging.
- Multi-tenant runtime policy.
- MCP-compatible tool exposure.

## Development

Add runtime dependencies with:

```bash
uv add <package>
```

Add development dependencies with:

```bash
uv add --dev <package>
```

Commit both `pyproject.toml` and `uv.lock` after dependency changes.
