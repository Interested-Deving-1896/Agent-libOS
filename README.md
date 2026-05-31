# Agent libOS

An experimental Agent-native libOS runtime written in Python.

Agent libOS models an agent as a long-running, schedulable, interruptible, capability-controlled `AgentProcess`, not as a single chat request or workflow thread. The codebase is an MVP implementation of the ideas in [agent_libos_design_doc.md](agent_libos_design_doc.md).

This project is still in active development.

## Current MVP

### Runtime

- Agent process lifecycle: `spawn`, `fork`, `exec`, `wait`, `signal`, `pause`, `resume`, `exit`.
- Async process supervisor: `Runtime.arun_until_idle()` automatically keeps runnable processes moving.
- Child process tools can fork workers, wait/join, list direct children, signal direct children, and merge child memory.
- Human queue integration is part of the runtime supervisor by default. If a primitive blocks on human approval, the process enters `WAITING_HUMAN`; the runtime processes human terminal messages, wakes the process, and resumes the pending action.
- Child waits are also resumable: `wait_child_process` puts the parent in `WAITING_EVENT`, child exit wakes the parent, and the original wait action resumes without asking the model for a new action.
- Single-step APIs remain available for tests and debugging: `run_next_process_once()` / `arun_next_process_once()` do not drain the human queue.
- Agent images configure process-visible tool tables at process creation time.
- Event bus and audit trace cover process, object memory, capabilities, tools, human requests, checkpoints, and external primitive access.
- SQLite stores process/object metadata, events, audit records, capabilities, human requests, tools, candidates, and checkpoints.

### Object Memory

- Typed Object Memory with handles, links, views, materialized context, snapshots, and merge scaffolding.
- Every Object has a globally unique `name`; authorized processes can resolve by name, but a name is not itself a capability.
- Object payloads live in runtime memory, not SQLite. SQLite stores directory metadata and a runtime-memory marker only.
- Process-owned memory is released on process exit unless retained as the process result.
- File/Object bridge tools can move file content into and out of Object Memory without returning the concrete content to the process-visible tool result.

### Tools And Primitives

LLM-facing tools are stable wrappers over libOS primitives. They are similar to libc calls: ergonomic and model-facing, but not the security boundary.

Built-in tools currently include:

- `create_memory_object`
- `create_object_from_file`
- `fork_child_process`
- `write_object_to_file`
- `wait_child_process`
- `list_child_processes`
- `signal_child_process`
- `merge_child_memory`
- `get_current_time`
- `sleep`
- `read_text_file`
- `write_text_file`
- `read_directory`
- `write_directory`
- `delete_file`
- `delete_directory`
- `request_permission`
- `ask_human`
- `human_output`
- `parse_pytest_log`
- `process_exit`
- `echo`

Important boundary rules:

- A process can call only tools in its process tool table.
- Tool call visibility is not an external-resource grant.
- Filesystem read/write/delete checks happen in the filesystem primitive.
- Human output and human approval checks happen in the HumanObject primitive.
- `ask_human` creates a blocking HumanObject question and returns the answer only after the human queue responds.
- Clock `sleep` is async, so one sleeping process does not block other runnable processes.

### Permissions And Human Queue

Permission requests are ordinary process actions mediated by the human queue:

- `request_permission` asks the human to choose a policy for a resource/right pair.
- The human can choose `always_allow`, `always_deny`, or `ask_each_time`.
- With `ask_each_time`, the relevant primitive creates a per-use human approval request when the operation is attempted.
- Per-use approval grants a one-shot capability that is consumed after one successful primitive call.
- Filesystem capabilities can target exact files such as `filesystem:workspace:README.md`, directory subtrees such as `filesystem:workspace:agent_outputs/*`, or the whole workspace.
- Runtime helpers can grant file/directory allow lists separately for read, write, and delete operations.
- Child processes inherit no external-resource capability by default; `fork_child_process` can explicitly inherit selected file, directory, or resource capabilities that the parent already holds.
- Ordinary human questions use the same queue: a process waiting on `ask_human` stays in `WAITING_HUMAN` until the terminal queue supplies an answer.
- Rejection does not crash the runtime; the process resumes and can report why it could not complete.
- Approval context includes path, resource, overwrite risk, byte count, SHA-256, target state, and a `repr()`-escaped content preview.

### LLM Execution

- OpenAI-compatible chat completions client using `.env` configuration.
- OpenAI tool-call schemas generated from the current process tool table.
- The runtime executes the selected legal tool call for each quantum.
- Free-form model text is allowed, but only tool calls or fallback JSON actions have side effects.
- Model calls run off the event loop, and tool dispatch has async support.

### Security Properties Covered By Tests

- Object handles are capability-protected; OIDs or object names alone do not grant access.
- Tool tables and external-resource capabilities are independent.
- Tools cannot bypass filesystem or human primitive checks.
- Path containment, revoked capabilities, fork attenuation, tool-table denial, JIT scope, and dangerous JIT imports are covered by tests.
- Built-in LLM-facing tools are checked so they do not directly touch host filesystem, terminal, network, shell, database, or secrets.

## Quick Start

Install dependencies:

```bash
uv sync
```

Run tests:

```bash
uv run python -m unittest discover -s tests -v
```

Run the deterministic local demo:

```bash
uv run agent-libos demo
```

The demo does not call a real model. It covers process spawn/fork, Object Memory, a JIT parser, checkpointing, capability denial before grant, human approval, filesystem write, final report object creation, and audit trace generation.

Use a persistent local runtime database:

```bash
uv run agent-libos --db .agent_libos.sqlite init
uv run agent-libos --db .agent_libos.sqlite demo
uv run agent-libos --db .agent_libos.sqlite audit
uv run agent-libos --db .agent_libos.sqlite processes
uv run agent-libos --db .agent_libos.sqlite tools
```

## LLM Configuration

Create a local `.env` file for real-model execution:

```bash
OPENAI_CODING_AGENT_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
OPENAI_LANGUAGE_MODEL=qwen3.7-max
OPENAI_API_KEY=...
```

Spawn and run a process:

```bash
uv run agent-libos --db .agent_libos.sqlite spawn --image coding-agent:v0 --goal "Write a short summary of README.md"
uv run agent-libos --db .agent_libos.sqlite run --max-quanta 10
```

`agent-libos run` uses the high-level async supervisor, so human terminal messages are processed as part of runtime execution. For manual queue processing, the lower-level command still exists:

```bash
uv run agent-libos --db .agent_libos.sqlite human
```

## Example Scripts

Summarize a workspace document through an Agent process:

```bash
uv run python scripts/llm_summarize_document.py README.md --auto-approve
```

Choose the permission policy explicitly for non-interactive runs:

```bash
uv run python scripts/llm_summarize_document.py README.md --permission-policy always_allow --auto-approve
uv run python scripts/llm_summarize_document.py README.md --permission-policy always_deny --auto-approve
```

Run the real-model write-file smoke test:

```bash
uv run python scripts/llm_write_goal_smoke.py
```

Copy a workspace text file through named Object Memory without materializing the file content into the process prompt:

```bash
uv run python scripts/object_memory_file_copy_smoke.py
```

Run two async-scheduled processes that use `sleep` to alternate current-time output:

```bash
uv run python scripts/async_clock_interleave_smoke.py --iterations 3 --interval 0.2
```

Expected output order is `A, B, A, B, ...`, showing that one process sleeping does not block the other process.

Ask the human which workspace file to view, then show that file's content:

```bash
uv run python scripts/ask_file_then_show.py
```

For non-interactive testing:

```bash
uv run python scripts/ask_file_then_show.py --auto-answer README.md
```

Run a traditional human/LLM terminal chat through the script-local `ChatImage`, using `ask_human` and `human_output`:

```bash
uv run python scripts/human_llm_chat.py
```

For a deterministic local smoke run without calling a model:

```bash
uv run python scripts/human_llm_chat.py --mock --auto-message hello --auto-message /exit
```

## Architecture

```text
Agent Personality / Application
  -> Skills / Tools Layer
     - LLM-facing actions
     - tool schemas
     - macro actions
     - skill metadata
  -> Agent libOS Runtime
     - AsyncProcessScheduler
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
     - terminal human queue
```

The key design boundary is between model-facing tools and libOS primitives. For example, `write_text_file` can be visible in a process tool table, but `FilesystemAdapter.write_text()` still enforces workspace containment, resource capability or permission policy, human approval if needed, events, and audit logging.

Putting a tool in a process table does not grant access to files, humans, shell, network, secrets, or other host resources.

## Runtime Execution Model

High-level execution:

```python
results = await runtime.arun_until_idle(max_quanta=10)
```

By default this does three things:

1. Runs all runnable processes asynchronously.
2. Processes pending human terminal messages when processes are waiting on human input.
3. Wakes resumed processes and continues until no runnable or human-resumable work remains, or the quantum budget is exhausted.

For debugging a pending approval state, opt out explicitly:

```python
results = await runtime.arun_until_idle(max_quanta=1, process_human_queue=False)
```

Single-step APIs also remain available:

```python
result = await runtime.arun_next_process_once()
```

## How To Write Agent libOS Tools

Tools should not directly access host resources. Use this pattern:

1. Define a Pydantic input schema and optional output schema.
2. Subclass `SyncAgentTool` for blocking local code or `BaseAgentTool` for async code.
3. Keep validation and model-facing ergonomics in the tool.
4. Call `ctx.runtime.<primitive>` for process, memory, filesystem, human, clock, or other libOS operations.
5. Let primitives enforce capability checks, containment, audit, event emission, checkpointing, and policy hooks.
6. Register the tool through `Runtime._register_builtin_tools()` or a ToolBroker-backed registry.

Do not put direct filesystem, terminal, network, shell, browser, database, or credential access inside a model-facing tool unless that code is itself the libOS primitive or a sandbox backend.

## Module Map

```text
agent_libos/
  api/             CLI entry points and demo orchestration
  capability/      Capability grant, revoke, check, and object handles
  external/        External-object primitives such as filesystem and clock
  human/           HumanObject query, approval, interrupt, and output primitives
  images/          Built-in AgentImage definitions
  llm/             Prompt, context, OpenAI-compatible client, executor, action parser
  memory/          Typed Object Memory and MemoryView implementation
  runtime/         Runtime composition, async scheduler, process manager, events, checkpoints, audit
  skills/          Skill schema, registry, verifier, linker scaffolding
  skills_tools/    Tool/action registry and bundle scaffolding
  storage/         SQLite persistence
  tools/           Tool base classes, ToolBroker, sandbox, and built-in tools
scripts/           Real-model smoke and demo scripts
tests/             Safety-boundary and regression tests
```

## Roadmap

Near-term priorities:

- LLM executor conformance tests for provider edge cases and tool-call formats.
- Tool result compaction and long-context paging.
- Stronger checkpoint/rollback tests.
- Audit querying by pid, capability, tool, external resource, and time range.
- More complete terminal human queue UX.
- Production-grade sandbox profiles for JIT and high-risk tools.

Longer-term directions:

- Persistent signed skill/tool registry.
- Distributed process scheduling.
- Rich human role and authority model.
- ExternalRef objects and snapshots for external resources.
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
