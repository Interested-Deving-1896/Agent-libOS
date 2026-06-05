# Agent libOS

An experimental Agent-native libOS runtime written in Python.

Agent libOS models an agent as a long-running, schedulable, interruptible, capability-controlled `AgentProcess`, not as a single chat request or workflow thread. The README is the current implementation guide. [agent_libos_design_doc.md](agent_libos_design_doc.md) is a historical design archive and may describe planned or superseded interfaces.

This project is still in active development.

Submission-facing M0 docs:

- [docs/invariants.md](docs/invariants.md) maps runtime invariants to regression coverage and known gaps.
- [docs/artifact_anonymity.md](docs/artifact_anonymity.md) tracks license, double-blind, and artifact hygiene checks.
- [docs/paper_thesis.md](docs/paper_thesis.md) freezes the current one-page paper story.
- [benchmarks/runtime_safety/schema.md](benchmarks/runtime_safety/schema.md) defines the M1 benchmark task schema v0.

## Current MVP

### Runtime

- Agent process lifecycle: `spawn`, `fork`, `exec`, `wait`, `signal`, `pause`, `resume`, `exit`.
- Async process supervisor: `Runtime.arun_until_idle()` automatically keeps runnable processes moving.
- Child process tools can fork workers, spawn fresh children, wait/join, list direct children, signal direct children, and merge child memory.
- Each process gets its own default Object Memory namespace at spawn/fork time. Bare Object Memory names resolve inside that process namespace.
- Each process has its own workspace-relative working directory. Relative filesystem paths and shell subprocess cwd resolve from that process cwd; the runtime host process does not `chdir` into launched workspaces.
- Each process has a durable message queue for IPC. Messages carry `kind`, `channel`, `correlation_id`, `reply_to`, subject/body, and structured payload; receivers can read, acknowledge, or block on selective filters.
- Human queue integration is part of the runtime supervisor by default. If a primitive blocks on human approval, the process enters `WAITING_HUMAN`; the runtime processes human terminal messages, wakes the process, and resumes the pending action.
- Child waits are also resumable: `wait_child_process` puts the parent in `WAITING_EVENT`, child exit wakes the parent, and the original wait action resumes without asking the model for a new action.
- Single-step APIs remain available for tests and debugging: `run_next_process_once()` / `arun_next_process_once()` do not drain the human queue.
- Agent images configure process-visible tool tables at process creation time.
- Event bus and audit trace cover process, process messages, object memory, capabilities, tools, human requests, checkpoints, and primitive access.
- SQLite stores process/object metadata, process messages, full LLM call records, events, audit records, capabilities, human requests, tools, candidates, and scoped checkpoints.
- LibOS primitives use an injectable Resource Provider Substrate. The default substrate is local host OS backed, but filesystem, clock/sleep, shell, and human terminal I/O providers can be replaced without changing tool schemas or capability checks.

### Object Memory

- Typed Object Memory with handles, namespace-local names, namespace directories, links, views, materialized context, snapshots, and merge scaffolding.
- The default namespace is process-private: process `proc_abc` resolves bare names inside `process:proc_abc`, similar to how an OS process sees its own virtual address space by default.
- Names are unique only inside a namespace. The same local name can exist independently in two process namespaces or in an explicit shared namespace.
- Explicit namespaces are directory-like scopes created with `create_memory_namespace` and inspected with `list_memory_namespace`.
- Namespace capabilities gate listing and name resolution. Object capabilities still gate reading, writing, linking, materializing, deleting, and granting object access.
- A name is not itself a capability: resolving `namespace/name` requires namespace read authority and object read authority.
- Object payloads live in runtime memory, not ordinary SQLite object rows. SQLite stores directory metadata and a runtime-memory marker only; checkpoint payloads are the explicit durable snapshot exception.
- Process-owned memory is released on process exit unless retained as the process result.
- File/Object bridge tools can move file content into and out of Object Memory without returning the concrete content to the process-visible tool result.

### Tools And Primitives

LLM-facing tools are stable wrappers over libOS primitives. They are similar to libc calls: ergonomic and model-facing, but not the security boundary.

Built-in tools currently include:

- `append_memory_object`
- `ask_human`
- `create_checkpoint`
- `create_memory_namespace`
- `create_memory_object`
- `create_object_from_file`
- `delete_directory`
- `delete_file`
- `diff_checkpoint`
- `discover_skills`
- `exec_process`
- `fork_checkpoint`
- `fork_child_process`
- `get_current_time`
- `get_working_directory`
- `human_output`
- `inspect_checkpoint`
- `inspect_skill`
- `load_image_from_yaml`
- `load_skill`
- `load_skill_from_yaml`
- `list_child_processes`
- `list_checkpoints`
- `list_memory_namespace`
- `merge_child_memory`
- `parse_pytest_log`
- `process_exit`
- `propose_jit_tool`
- `read_directory`
- `read_memory_object`
- `read_process_messages`
- `receive_process_messages`
- `read_text_file`
- `register_jit_tool`
- `request_permission`
- `restore_checkpoint`
- `run_shell_command`
- `send_process_message`
- `set_working_directory`
- `signal_child_process`
- `sleep`
- `spawn_child_process`
- `unload_skill`
- `validate_jit_tool`
- `wait_child_process`
- `write_directory`
- `write_object_to_file`
- `write_text_file`
- `echo`

Important boundary rules:

- A process can call only tools in its process tool table.
- Tool call visibility is not an external-resource grant.
- Bare Object Memory names resolve in the caller's process namespace; shared memory requires an explicit namespace plus namespace/object capabilities.
- Relative filesystem paths and shell commands resolve from the caller's process working directory, which is independent for each `AgentProcess`.
- Filesystem read/write/delete checks happen in the filesystem primitive.
- Human output, human questions, and human approval checks happen in the HumanObject primitive; concrete terminal reads/writes happen only through the substrate `HumanProvider`.
- Shell execution checks happen in the shell primitive. The model-facing tool accepts argv arrays only; it never accepts shell command strings for implicit parsing.
- Image registration checks happen in the image registry primitive. `load_image_from_yaml` only reads a workspace YAML file and passes the parsed manifest to that primitive.
- `ask_human` creates a blocking HumanObject question and returns the answer only after the human queue responds.
- Clock `sleep` is async, so one sleeping process does not block other runnable processes.
- Agent-authored JIT tools are Deno/TypeScript modules. They export `run(args, libos)` and can reach libOS only through `await libos.syscall(name, args)`.
- JIT syscalls do not consult the caller's LLM-facing tool table. They are authorized by pid, primitive-level capabilities, permission policy, human approval, and audit.
- The Deno subprocess is launched with `--no-prompt` and no read/write/net/env/run/ffi host permissions. Static imports are limited to configured `jsr:` packages, with a small `@std/*` allowlist by default.
- Human approval is part of a syscall. TypeScript sees either the final syscall payload or a final syscall error; it never sees a pending/retry protocol state.
- `process.exit` and `process.exec` are ordinary syscalls from the TypeScript side. The runtime applies the resulting lifecycle change only after the JIT tool returns its normal tool result.

### Skills

Skills are dynamic, capability-controlled model-facing packages. A Skill can
provide prompt instructions, action summaries, existing tool references, and
optional Deno/TypeScript JIT tool candidates. Loading a Skill changes only the
current process tool table and prompt materialization. It never grants
filesystem, shell, human, Object Memory, process, image, checkpoint, or other
resource capabilities.

Skill manifests use schema version `1` and may be YAML or JSON, either as direct
fields or under a top-level `skill:` mapping:

```yaml
skill:
  schema_version: 1
  skill_id: review-helper:v0
  name: Review Helper
  version: v0
  description: Focused code-review workflow helpers.
  instructions: |
    Prefer small, evidence-backed findings with file and line references.
  tools:
    - read_text_file
    - read_directory
  actions:
    - name: summarize_findings
      use_cases:
        - Summarize review findings in severity order.
      input_schema:
        type: object
        properties:
          findings:
            type: array
      output_schema:
        type: object
  jit_tools: []
  required_capabilities:
    - resource: filesystem:workspace:*
      rights:
        - read
  metadata:
    owner: local
```

`required_capabilities` is advisory metadata for humans and prompts. The runtime
does not grant those capabilities during registration or load. If a loaded Skill
exposes `read_text_file`, that tool still fails at the filesystem primitive
without filesystem read authority.

Skill sources are split into workspace and global sources:

- Workspace Skills are read through the filesystem primitive, so the process
  must have read authority for the manifest path. If the process lacks
  `skill:<skill_id>` write/execute authority, loading can go through the normal
  human approval path and one-shot grant.
- Global Skills are read only from configured global Skill directories. The
  exact manifest bytes must match a SHA-256 allowlist entry or a row in the
  `skill_trust` table before registration or load.

JIT tools bundled in a Skill use the same Deno sandbox, import allowlist,
ToolBroker registration path, and syscall broker as manually proposed JIT tools.
They are visible only to the loading process and cannot shadow static tool names.

CLI examples:

```bash
uv run agent-libos --db .agent_libos.sqlite skills trust ~/.agent-libos/skills/review.yaml
uv run agent-libos --db .agent_libos.sqlite skills register ~/.agent-libos/skills/review.yaml --source-type global
uv run agent-libos --db .agent_libos.sqlite skills discover
uv run agent-libos --db .agent_libos.sqlite skills load <pid> review-helper:v0 --actor-pid <pid>
uv run agent-libos --db .agent_libos.sqlite skills unload <pid> review-helper:v0 --actor-pid <pid>
```

Processes can also use the LLM-facing `discover_skills`, `inspect_skill`,
`load_skill`, `load_skill_from_yaml`, and `unload_skill` tools when those tools
are visible. Deno JIT tools can call `skill.discover`, `skill.inspect`,
`skill.register`, `skill.load`, `skill.unload`, and `skill.load_yaml` syscalls;
syscalls go to the Skill primitive and do not consult the LLM-facing tool table.

### Checkpoints

- Checkpoints are capability-controlled durable snapshots of reconstructable runtime state for one process subtree.
- A checkpoint captures process state, Object Memory metadata and payloads, process namespaces, object links, subtree capabilities, tool/JIT/Skill metadata, mailbox delivery state, and image definitions needed by that subtree.
- Restore is scoped to the checkpoint owner subtree. It does not delete audit records, events, LLM call records, checkpoint records, or human interaction history.
- External filesystem, shell, image, network, and provider effects are not rolled back. Restore reports them in `external_effects_since_checkpoint` for audit/explain or future compensation.
- Default images expose low-risk `create_checkpoint`, `list_checkpoints`, `inspect_checkpoint`, and `diff_checkpoint`; `restore_checkpoint` and `fork_checkpoint` are registered but require explicit tool visibility plus checkpoint authority.
- CLI admin commands are available with `agent-libos checkpoint create|list|inspect|diff|restore|fork|replay`. Passing `--actor-pid` makes the CLI enforce that process's checkpoint capabilities.

### Permissions And Human Queue

Permission requests are ordinary process actions mediated by the human queue:

- `request_permission` asks the human to choose a policy for a resource/right pair.
- The human can choose `always_allow`, `always_deny`, or `ask_each_time`.
- With `ask_each_time`, the relevant primitive creates a per-use human approval request when the operation is attempted.
- Per-use approval grants a one-shot capability that is consumed after one successful primitive call.
- Filesystem capabilities can target exact files such as `filesystem:workspace:README.md`, directory subtrees such as `filesystem:workspace:agent_outputs/*`, or the whole workspace.
- Shell capabilities are process-scoped policies over `shell:*`. The built-in policy levels are `always_deny`, `allowlist_auto_else_ask`, `blocklist_ask_else_auto`, and `always_allow`; `always_allow` is intentionally marked high-risk.
- Shell allow/block lists match tokenized argv, not substrings, globs, or shell-expanded strings. Allow-list rules are exact by default, bare executable names do not match path-qualified executables, and block-list checks also scan nested executable-looking argv tokens such as `bash` or `powershell`.
- Runtime helpers can grant file/directory allow lists separately for read, write, and delete operations.
- Child processes inherit no external-resource capability by default; `fork_child_process` and `spawn_child_process` can explicitly inherit selected file, directory, or resource capabilities that the parent already holds.
- `fork_child_process` attenuates a selected parent MemoryView into the child. `spawn_child_process` creates a fresh direct child with a new process namespace and a goal-only MemoryView.
- `exec_process` replaces the current process image and tool table without changing pid. It never grants the target image's required capabilities automatically; capabilities are preserved only when explicitly requested, otherwise external capabilities are shrunk.
- Image registration requires `write` on `image:<image_id>` or a wildcard such as `image:*`. The YAML loader also requires filesystem read authority for the manifest path.
- Skill registration/loading requires Skill authority and source authority, but Skill manifests do not grant their declared `required_capabilities`.
- Ordinary human questions use the same queue: a process waiting on `ask_human` stays in `WAITING_HUMAN` until the terminal queue supplies an answer.
- Rejection does not crash the runtime; the process resumes and can report why it could not complete.
- Approval context includes path, resource, overwrite risk, byte count, SHA-256, target state, and a `repr()`-escaped content preview.

### LLM Execution

- OpenAI-compatible LLM client using `.env` configuration.
- OpenAI tool-call schemas generated from the current process tool table.
- The runtime executes the selected legal tool call for each quantum.
- Free-form model text is allowed, but only tool calls or fallback JSON actions have side effects.
- Malformed tool calls with missing function names are rejected; when possible the executor gives the model one repair attempt with the exact visible tool names.
- Model calls run off the event loop, and tool dispatch has async support.
- Each process LLM context is stored as a mutable Object Memory object named `llm_context:<pid>`. The runtime appends new process facts, events, capability snapshots, and object summaries to the end of this object so repeated prompt prefixes remain stable for prompt caching.

### Built-In Coding Image

`coding-agent:v0` is the practical repository-engineering image. It starts with read-only workspace authority and human-output authority, but no default write/delete authority. Its prompt tells the agent to scale the size of a change to the goal, preserve plans and evidence in Object Memory, fork child workers only when parallel analysis materially helps, spawn fresh children when parent context should not be copied, use pregranted write/delete authority when present, request least-privilege permissions when authority is missing, use file/Object bridge tools for large content movement, parse pytest logs when available, and exit with a structured summary of changes, evidence, verification, residual risks, and follow-up.

### Security Properties Covered By Tests

- Object handles are capability-protected; OIDs or object names alone do not grant access.
- Object Memory namespaces are capability-protected; namespace read/write and object read/write are separate checks.
- Tool tables and external-resource capabilities are independent.
- Tools cannot bypass filesystem or human primitive checks.
- Path containment, revoked capabilities, fork attenuation, spawn-child isolation, exec non-escalation, image registration authority, tool-table denial, Deno/TypeScript JIT scope, syscall capability checks, human approval inside syscalls, deferred process lifecycle, and unsafe import/API rejection are covered by tests.
- Built-in LLM-facing tools are checked so they do not directly touch host filesystem, terminal, network, shell, database, or secrets.

## Quick Start

Install dependencies:

```bash
uv sync
```

Deno is optional for the Python test suite. Install `deno` or set `agent_libos.config.DEFAULT_CONFIG.tools.deno_executable` if you want to validate or run real Deno/TypeScript JIT tools.

Run tests:

```bash
uv run python -m unittest discover -s tests -v
```

Run the deterministic M1 runtime-safety benchmark subset:

```bash
uv run python experiments/run_benchmark.py --suite benchmarks/runtime_safety --runner agent_libos_full --limit 3 --output .benchmark_runs/m1-smoke
uv run python experiments/collect_metrics.py .benchmark_runs/m1-smoke
```

The benchmark harness defaults to mock/planned actions. A real-model smoke run is available only when LLM environment variables are configured, and must be scoped explicitly with `--llm real --limit 1` or a single `--task`.

Run the deterministic local demo:

```bash
uv run agent-libos demo
```

The demo does not call a real model. It covers process spawn/fork, Object Memory, a Deno/TypeScript JIT parser when Deno is available, checkpointing, capability denial before grant, human approval, filesystem write, final report object creation, and audit trace generation. If Deno is not installed, the demo reports the JIT validation error and continues through the rest of the contract.

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
OPENAI_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
OPENAI_LANGUAGE_MODEL=qwen3.7-max
OPENAI_API_KEY=...
```

The LLM client uses the OpenAI Python SDK. By default it uses the Responses API for OpenAI-hosted models and falls back to Chat Completions for custom OpenAI-compatible `base_url` providers. Set `OPENAI_API_MODE=responses` or `OPENAI_API_MODE=chat` to force a mode. Optional knobs include `OPENAI_TIMEOUT`, `OPENAI_MAX_RETRIES`, `OPENAI_STORE`, `OPENAI_REASONING_EFFORT`, `OPENAI_VERBOSITY`, and provider-specific `OPENAI_ENABLE_THINKING`.

Runtime defaults that are not provider secrets live in `agent_libos.config.DEFAULT_CONFIG`. This includes scheduler quanta, process budgets, default image ids, workspace namespace, tool timeouts, filesystem/object-memory size limits, Deno JIT sandbox limits, JSR import allowlists, shell policy lists, launcher presets, and example-script defaults. Components accept an `AgentLibOSConfig` where runtime-level injection is useful; fixed protocol identifiers and model-facing tool semantics stay in their own modules.

Spawn and run a process:

```bash
uv run agent-libos --db .agent_libos.sqlite spawn --image coding-agent:v0 --goal "Write a short summary of README.md"
uv run agent-libos --db .agent_libos.sqlite run --max-quanta 10
```

`agent-libos run` uses the high-level async supervisor, so human terminal messages are processed as part of runtime execution. For manual queue processing, the lower-level command still exists:

```bash
uv run agent-libos --db .agent_libos.sqlite human
```

Every LLM action-selection call is persisted in SQLite as an `llm_calls` row. The record includes the exact prompt messages, visible tool schemas, output content, tool calls, provider ids, model/api, token usage when the provider returns it, reasoning fields when exposed by the provider, raw response JSON, and errors. Inspect them with:

```bash
uv run agent-libos --db .agent_libos.sqlite llm-calls --pid <pid>
```

Humans can also inject process messages at any time. This works while another `agent-libos run` is using the same SQLite runtime database:

```bash
uv run agent-libos --db .agent_libos.sqlite message <pid> "Please inspect the latest result"
uv run agent-libos --db .agent_libos.sqlite interrupt <pid> "Stop current work and read this first"
uv run agent-libos --db .agent_libos.sqlite message <pid> "Use this as job input" --channel human --correlation-id job-42 --run
```

For a Codex CLI-style loop in one terminal, use interactive run. Plain text sends a normal message unless a human question or approval is pending, in which case it answers that request; use `/message <text>` to force a normal process message. `/interrupt <text>` sends an interrupt; `/pid <pid>` switches the target; `/exit` exits the interactive loop.

```bash
uv run agent-libos --db .agent_libos.sqlite run --interactive --pid <pid> --max-quanta 20
```

The CLI also exposes process built-ins for manual lifecycle control:

```bash
uv run agent-libos --db .agent_libos.sqlite cd <pid> src
uv run agent-libos --db .agent_libos.sqlite exec image.yaml "Review README.md" --pid <pid> --run
uv run agent-libos --db .agent_libos.sqlite exit <pid> --payload '{"done":true}'
```

For `exec`, the first positional argument is the target image. It can be an already registered image id such as `coding-agent:v0`, or a `.yaml` / `.yml` AgentImage manifest path such as `image.yaml`. The second positional argument is the replacement goal. `--run` runs the scheduler immediately after exec; omit it or pass `--no-run` to only swap the process image and tool table.

An AgentImage YAML manifest accepted by `load_image_from_yaml` can use either a top-level image mapping or direct image fields:

```yaml
image:
  image_id: yaml-agent:v0
  name: yaml-agent
  system_prompt: |
    Use the smallest safe tool sequence.
  default_tools:
    - read_memory_object
    - human_output
  context_policy: evidence_first
  safety_profile: review
  metadata:
    role: example
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

Launch a real coding agent against any workspace with preconfigured permissions:

```bash
uv run python scripts/run_coding_agent.py --workspace /path/to/repo --goal "Implement the requested change"
```

On Windows PowerShell, the same launcher works with Windows-style paths:

```powershell
uv run python scripts\run_coding_agent.py --workspace ..\some-repo --goal "Summarize the current project"
```

The launcher defaults to the `edit` permission preset: read+write over the workspace, but no delete authority. Use `--permission-preset read-only` for inspection-only runs, `--permission-preset full` for read+write+delete, or combine `read-only` with exact allow-list grants such as `--write-file src/main.py` and `--delete-dir build`.

The launcher also grants a shell policy by default: `--shell-policy allowlist_auto_else_ask`. Use `--shell-policy none` to grant no shell execution policy, `always_deny` to hard-disable shell calls, `blocklist_ask_else_auto` to auto-allow commands except configured risky entries, or `always_allow` only for high-risk fully trusted runs.

By default the launcher loads LLM settings from this Agent-libOS checkout's `.env` before mounting the target workspace into the Resource Provider Substrate. It does not change the launcher process cwd. Use `--env-file /path/to/.env` to override that.

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
     - dynamic Skill manifests and prompt instructions
     - process-visible JIT tool candidates
  -> Agent libOS Runtime
     - AsyncProcessScheduler
     - ProcessManager
     - ObjectMemoryManager
     - ToolBroker
     - HumanObjectManager
     - Primitive managers
     - CapabilityManager
     - EventBus
     - CheckpointManager
     - AuditManager
  -> Resource Provider Substrate
     - filesystem provider
     - clock/sleep provider
     - shell provider
     - human provider
  -> Host Runtime / Provider Backend
     - local workspace filesystem
     - host clock
     - subprocess backend
     - terminal or UI human I/O backend
     - future remote, container, WASM, or service-backed providers
```

The key design boundary is between model-facing tools and libOS primitives. For example, `write_text_file` can be visible in a process tool table, but `FilesystemAdapter.write_text()` still enforces workspace containment, resource capability or permission policy, human approval if needed, events, and audit logging.

Putting a tool in a process table does not grant access to files, humans, shell, network, secrets, or other host resources.

Loading a Skill follows the same rule. A Skill can add existing tools or
validated JIT tools to one process table and can add bounded instructions to the
next prompt, but resource authority remains entirely in primitive capabilities
and policy. Global Skill trust and workspace Skill filesystem reads are checked
before the Skill registry is modified.

Primitives are not themselves the host implementation. They own libOS semantics: capability checks, human approval, event emission, and audit records. Concrete host calls live behind `agent_libos.substrate` providers such as `LocalFilesystemProvider`, `LocalClockProvider`, `LocalShellProvider`, and `LocalHumanProvider`. Shell calls are intentionally argv-only at this boundary, so quoting, pipes, redirects, and command chaining must be requested explicitly through an interpreter executable, where policy matching can see the interpreter token. HumanObject similarly owns request queues, approvals, wakeups, and audit records, while the substrate `HumanProvider` owns terminal or UI read/write.

## Runtime Execution Model

High-level execution:

```python
results = await runtime.arun_until_idle(max_quanta=10)
```

By default this does four things:

1. Runs all runnable processes asynchronously.
2. Processes pending human terminal messages when processes are waiting on human input.
3. Delivers process-message notices at the appropriate tool boundary.
4. Wakes resumed processes and continues until no runnable or human-resumable work remains, or the quantum budget is exhausted.

Process messages are explicit queue entries, not raw prompt text. A process can send messages to itself, its parent, or direct children with `send_process_message`. The receiver uses `read_process_messages` for non-blocking inspection or `receive_process_messages` to wait in `WAITING_EVENT` until a matching unread message arrives. Both read paths can filter by kind, sender, channel, correlation id, reply target, or exact message ids, and returned unread messages are acknowledged by default. Interrupt messages are checked before tool execution and preempt non-message tools until read; normal messages are noticed after a tool call and do not block the current tool.

For debugging a pending approval state, opt out explicitly:

```python
results = await runtime.arun_until_idle(max_quanta=1, process_human_queue=False)
```

Single-step APIs also remain available:

```python
result = await runtime.arun_next_process_once()
```

## Object Memory Namespace Model

Object Memory names are local to a namespace. Runtime code that omits `namespace` uses the caller process namespace:

```python
pid = runtime.process.spawn(image="base-agent:v0", goal="collect notes")
handle = runtime.memory.create_object(
    pid=pid,
    object_type="summary",
    name="notes",
    payload={"entries": []},
    immutable=False,
)
obj = runtime.memory.get_object_by_name(pid, "notes")
assert obj.namespace == runtime.memory.process_namespace(pid)
```

For shared or phase-specific memory, create an explicit namespace and pass it on object operations:

```python
runtime.memory.create_namespace(pid, "project")
runtime.memory.create_namespace(pid, "project/research")
runtime.memory.create_object(
    pid=pid,
    object_type="observation",
    namespace="project/research",
    name="notes",
    payload={"source": "README.md"},
)
listing = runtime.memory.list_namespace(pid, "project/research")
```

The namespace grants directory-style authority such as list, lookup, and create. It does not replace object capabilities; reading `project/research/notes` still requires object read capability.

## How To Write Agent libOS Tools

Tools should not directly access host resources. Use this pattern:

1. Define a Pydantic input schema and optional output schema.
2. Subclass `SyncAgentTool` for blocking local code or `BaseAgentTool` for async code.
3. Keep validation and model-facing ergonomics in the tool.
4. Call `ctx.runtime.<primitive>` for process, memory, filesystem, human, clock, or other libOS operations.
5. Let primitives enforce capability checks, containment, audit, event emission, checkpointing, and policy hooks.
6. Register the tool through `Runtime._register_builtin_tools()` or a ToolBroker-backed registry.

Do not put direct filesystem, terminal, network, shell, browser, database, or credential access inside a model-facing tool unless that code is itself the libOS primitive or a sandbox backend.

Agent-authored JIT tools use TypeScript, not Python. A process proposes source with `propose_jit_tool`, validates it with `validate_jit_tool`, and registers it with `register_jit_tool`. Registration adds the new tool only to the registering process tool table.

The TypeScript source shape is:

```ts
export async function run(args, libos) {
  const file = await libos.syscall("filesystem.read_text", { path: args.path });
  return { bytes: file.content.length };
}
```

The `libos` object intentionally exposes only `syscall(name, args)`. It does not expose Python objects, `Runtime`, or `runtime.tools`. Syscall dispatch enters `LibOSSyscallSession`, which calls primitives such as filesystem, Object Memory, human, clock, process, shell, and image registry under the caller pid.

Skills may bundle the same JIT candidate shape inside `jit_tools`. Skill loading
validates all existing-tool references and JIT candidates before changing the
process table. Unloading a Skill removes the tool visibility and prompt
instructions contributed by that Skill, but it does not revoke capabilities,
delete audit history, or undo external side effects.

## Module Map

```text
agent_libos/
  api/             CLI entry points and demo orchestration
  capability/      Capability grant, revoke, check, and object handles
  config/          Typed runtime, LLM, tool, memory, launcher, and script defaults
  human/           HumanObject query, approval, interrupt, and output primitives
  images/          Built-in AgentImage definitions
  llm/             Prompt, context, OpenAI-compatible client, executor, action parser
  memory/          Typed Object Memory and MemoryView implementation
  models/          Dataclass and enum models split by runtime domain
  primitives/      LibOS primitive managers for filesystem, clock, shell, git, and browser placeholders
  runtime/         Runtime composition, syscall broker, async scheduler, process manager, events, checkpoints, audit
  skills/          Skill schema, strict manifest loader, trust registry, and runtime SkillManager primitive
  substrate/        Resource provider interfaces for filesystem, clock, shell, human I/O, and local host-backed implementations
  storage/         SQLite persistence
  tools/           Tool base classes, ToolBroker, sandbox, and built-in tools
scripts/           Real-model smoke and demo scripts
tests/             Safety-boundary and regression tests
```

## Roadmap

Near-term priorities:

- More LLM executor conformance tests for provider edge cases and unusual tool-call formats.
- Tool result compaction and long-context paging.
- Audit explain over checkpoint restore/fork decisions and external effects since checkpoint.
- Audit querying by pid, capability, tool, external resource, and time range.
- More complete terminal human queue UX.
- More hardened Deno JIT sandbox profiles and policy presets for high-risk tools.
- Richer Skill policy, provenance display, and public-key signature verification.

Longer-term directions:

- Distributed signed Skill/tool registries.
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
