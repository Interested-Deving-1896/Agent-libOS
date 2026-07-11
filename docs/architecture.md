# Architecture

Agent libOS is structured around one boundary: model-visible and
self-evolving action surfaces are not resource authority. A process may see a
tool schema, activate a Skill, register a JIT tool, register or exec an image,
fork a child, restore from a checkpoint, or inspect a remote endpoint, but
protected effects are authorized only when a primitive runs under that process
id.

## Layer Model

```text
Agent personality / application
  -> Skills and tools layer
     - model-facing actions
     - prompt instructions
     - tool schemas
     - Deno/TypeScript JIT candidates
  -> Agent libOS runtime
     - trusted startup module loader
     - scheduler
     - process manager
     - Object Memory manager
     - ToolBroker
     - Skill manager
     - HumanObject manager
     - primitive managers
     - capability manager
     - event bus
     - checkpoint manager
     - operation/explain manager
     - audit manager
  -> Resource Provider Substrate
     - filesystem provider
     - clock provider
     - shell provider
     - human provider
     - JSON-RPC over HTTP provider
     - MCP client provider
  -> host backend
     - local workspace filesystem
     - host clock
     - subprocess backend
     - terminal or UI human I/O
     - pre-registered remote JSON-RPC endpoints
     - pre-registered MCP servers
     - future container, WASM, or service providers
```

The Skills and tools layer exists for LLM ergonomics and self-evolution. It
presents stable action names, schemas, summaries, workflow instructions, and
process-local JIT candidates. It does not own external authority.

Image registration and `exec` are also self-evolution mechanisms. They can
change a process prompt, prompt composition mode, default tool table, default
Skills, and lifecycle shape, but image visibility and target-image metadata do
not grant resource capabilities or impose resource budgets. Launch-time callers
provide a durable [`TaskAuthorityManifest`](task_authority_manifest.md) that
owns capability, effect-class, approval-policy, and resource-budget ceilings.
Image `required_capabilities` are unmet-requirement declarations, not grants.
Image packages may seed a
private per-process workspace and process-local JIT tools, but those are scoped
to the booted process and do not expose the package source directory.
Default tool tables are exact image declarations: the runtime does not add
generic lifecycle or Object Memory tools unless the image lists them.
Images may opt into lazy model projection. The complete image tool table stays
callable and capability-enforced, while a separate durable model projection
initially exposes only discovery/core tools. Activating a group changes schemas
and visibility but never capabilities.

Startup Runtime Modules are different from Skills. A module is trusted Python
host code loaded before `Runtime.open()` returns. Modules extend the runtime
composition root by registering tools, images, syscalls, provider hooks, and
startup hooks. Because modules run in the host interpreter, they are part of
the runtime trusted computing base and are gated by manifest hash trust rather
than by process capabilities.

The runtime owns agent-level semantics: process identity, capability checks,
approval, event emission, audit, process wakeups, checkpointing, and durable
metadata.

Explainable Operations overlays these managers without replacing their source
records. A `ContextVar` carries the active causal operation through async calls
and `asyncio.to_thread`; protected public boundaries create typed parent/child
rows, while audit, event, capability reservation, Human request, provider
effect, resource charge, LLM call, and context materialization code attaches
explicit evidence ids. Query code follows only those links. It does not infer
causality from pid and time proximity. See
[explainable_operations.md](explainable_operations.md).

The Resource Provider Substrate owns concrete host calls. A provider is a
backend, not a security bypass. Replacing the filesystem or shell provider must
not change tool schemas or skip primitive authorization.

Providers are also the source of truth for successful external-effect rollback
classification. Effectful provider calls must expose a classifier to the
primitive; the runtime persists the result for checkpoint reports, but v1 does
not apply external compensation. Filesystem mutation, clock, shell, and PTY
spawn paths use explicit finite-use reservations around the provider boundary.
A filesystem, clock, shell, human-output/terminal-I/O, PTY, JSON-RPC, or live
MCP primitive also persists a conservative `unknown` external-effect intent
immediately before entering that boundary. On a classified success or
ambiguous failure, the store conditionally updates that same `effect_id` from
`pending` to `finalized`, matching pid, provider, operation, and target. An
already finalized, abandoned, or mismatched intent cannot be settled again. If
capability commit, event/audit, classification, or final persistence fails after
the provider may have run, the pending `unknown` row remains durable and is
visible to checkpoint, Explain, and benchmark consumers instead of creating a false
absence of evidence.

Each intent also records a canonical argument hash, idempotency key, and an
irreversible transaction state (`prepared`, `dispatched`, `committed`,
`failed`, `unknown`, or `compensated`). A unique process/idempotency-key index
blocks duplicate dispatch. Startup may call a provider reconciliation hook to
query an existing receipt; it never replays the effect. Providers without that
hook leave the transaction `unknown`.

A provider may raise `ProviderEffectNotStarted` only when it can certify that
its selected call did not begin. The primitive abandons the pending intent only
when no earlier provider observation in the composite operation produced
information flow. In that case it restores an exact reservation when one was
reserved; filesystem/clock/shell and PTY spawn perform restoration and
abandonment in one store transaction. If an earlier filesystem `state()` or MCP
live-tool validation already returned information, the main mutation/call being
not-started still finalizes an information-flow effect instead of erasing the
intent.

Human terminal reads and automatic writes persist only request/purpose and
length/hash observations; raw prompts, answers, and provider exception text do
not enter effect or audit metadata. JSON-RPC and non-local HTTP MCP persist the
intent and reserve deduplicated finite authority before DNS. Once DNS observes
the host, a later transport PENS cannot erase that information flow or restore
the use. Endpoint/server registry item authority is checked before metadata
lookup, and registry row, stale-grant, event, and audit mutations are atomic.

Clock sleep/asleep similarly starts its intent before the first `monotonic()`
measurement. Only a not-started result from that first observation may restore
and abandon; every later sleep, cancellation, or measurement failure consumes
the use and finalizes unknown. The successful elapsed-time result is also an
information flow.

Once the provider boundary is crossed, the reservation is committed. Timeout,
cancellation, resource-limit, ordinary provider exception, or post-effect
classifier failure cannot prove non-execution, so authority stays consumed and
the primitive records or retains a conservative `unknown` effect. This makes a
failed return value distinct from a proven absence of external effects.

The PTY Runtime Module applies this pending-to-finalized protocol to spawn,
write, resize, and close. Cleanup after a spawned session fails to publish its
Object is containment, not evidence that spawn never occurred; classifier
absence or failure after a PTY operation finalizes an `unknown` fallback, while
post-provider sink failure leaves the pending row visible.

## Composition Root

`agent_libos.runtime.runtime.Runtime` wires the runtime together:

- `RuntimeStore` persists metadata and append-only records through a backend
  abstraction. SQLite is the default backend; PostgreSQL is available through
  an optional extra. Both SQL backends share the same `SQLRuntimeStore`
  repository contract while backend classes own connection setup and dialect
  behavior.
- `RuntimeModuleRegistry` loads the internal core module and configured trusted
  startup modules before processes, tools, or LLM execution can run.
- `CapabilityManager` grants, checks, revokes, and consumes one-shot authority.
- `ObjectMemoryManager` provides typed memory and namespace resolution.
- `HumanObjectManager` owns questions, approvals, terminal queue processing,
  and human output.
- `FilesystemAdapter`, `ShellAdapter`, `ClockPrimitive`, `JsonRpcPrimitive`,
  and `McpPrimitive` expose protected primitive operations over provider
  backends.
- `ToolBroker` registers static tools and process-local JIT tools.
- `SkillManager` registers standard Skill packages and activates them into
  process tool tables and prompt context without granting resource authority.
- `ProcessManager` owns lifecycle, working directories, child relationships,
  and image transitions.
- `SimpleScheduler` runs runnable processes and wakes waiting work.
- `CheckpointManager` snapshots and restores reconstructable process-subtree
  state; checkpoint-derived image commit reuses that internal snapshot boundary.
- `LLMProcessExecutor` materializes prompt context, resolves the process
  `llm_profile_id` through the host profile registry, calls that LLM client,
  and dispatches selected tool calls.

The default substrate is `LocalResourceProviderSubstrate`, rooted at the current
workspace unless another substrate is injected.

The internal core module registers the built-in tool set and default images
through the same module registration path exposed to trusted external modules.
This keeps future providers, syscalls, and images from accumulating ad hoc
startup code in the composition root.

Host-facing control surfaces live under `agent_libos.api`. The CLI entrypoint
and the local GUI HTTP/SSE server are different presentations over the same
runtime managers and primitives; neither is an authority boundary by itself.
Both must call `Runtime.shutdown()` when they own a runtime instance. Shutdown
first stops scheduler work and ObjectTask runner work, then releases host
resources and emits runtime lifecycle audit/event records. If a synchronous
quantum or ObjectTask tool thread cannot be joined safely, shutdown reports the
component that did not stop and leaves owned storage open so a live worker is
not racing a closed runtime store connection. Host shutdown never marks AgentProcess
records as exited.

## Tool Boundary

LLM-facing tools are stable wrappers over primitives. For example,
`write_text_file` can be visible in a process tool table, but the actual write
still enters the filesystem primitive, which checks:

- workspace containment,
- process working directory resolution,
- filesystem capability or permission policy,
- human approval if policy requires it,
- overwrite and content preview metadata,
- event emission,
- audit recording.

Putting a tool in a process table never grants access to files, shell,
terminal/human I/O, Object Memory, image registration, checkpoints, or other
resources.

Likewise, `call_jsonrpc_method` visibility never grants network authority. The
JSON-RPC primitive accepts only endpoint and method ids, first gates on the
derived `jsonrpc:<endpoint>:<method>` capability resource without loading the
endpoint manifest, then resolves URLs and env-backed headers from the registry
only for an authorized call.

The same split applies to MCP. `list_mcp_servers`, `inspect_mcp_server`,
`list_mcp_tools`, and `call_mcp_tool` are stable generic wrappers over a
registered MCP server registry. Remote MCP tools are not imported into the
ToolBroker as first-class tools, and a visible `call_mcp_tool` entry still
requires `mcp:<server>:<tool>` authority at primitive use. The call path also
checks that derived tool resource before loading server metadata or input
schemas, so missing authority cannot be used to enumerate provider manifests.

## Primitive Boundary

Primitives are the runtime boundary. They are responsible for:

- authorizing the caller pid against capabilities and policy,
- blocking on human approval when needed,
- validating inputs before side effects,
- constraining provider paths, argv, sizes, and timeouts,
- emitting events,
- writing audit records,
- preserving process wake/resume semantics.

JIT syscalls enter the same primitive boundary through
`LibOSSyscallSession`. They do not consult the caller's LLM-facing tool table.
Trusted startup modules may add new syscall names through the runtime syscall
router, but module syscalls still execute as libOS syscalls under the caller
pid and must call primitives for protected effects.

Deno is released only after a dedicated supervisor establishes host-lifetime
process-tree containment: an inherited death pipe plus isolated process group
on POSIX, or a `KILL_ON_JOB_CLOSE` Job Object on Windows. Sandbox execution
fails closed if that containment cannot be established.

## Persistence And Audit

The runtime store keeps durable metadata and append-only records:

- processes, working directories, loaded Skills, and tool tables,
- Object Memory metadata and namespace directories,
- capabilities and object handles,
- process messages and human requests,
- tools and JIT candidates,
- Skill registry and trust rows,
- loaded Runtime Module status, source hashes, and registration summaries,
- image registry manifests and checkpoint-derived image artifacts,
- JSON-RPC endpoint registry rows,
- MCP server registry rows,
- checkpoints and checkpoint payload snapshots,
- durable LLM pending-action generations, Responses tool outputs, and context
  generations used to validate opt-in provider chaining,
- provider-decided finalized external effects and conservative pending intents,
- events and audit records,
- LLM call records with provider ids, model/API mode, usage, errors, and
  full prompt, visible tools, output, tool calls, reasoning metadata, raw
  response, and bounded observability envelopes. Full LLM input/output
  persistence is enabled by default for self-evolution training and
  fine-tuning pipelines; this may include sensitive prompt, tool, reasoning,
  and provider payload fields. Set `llm.persist_full_io: false` to opt out and
  store only previews plus hashes for those fields.

Object payloads are not ordinary durable object rows. They live in runtime
memory, while SQL object rows store only a runtime-memory marker. Rows whose
live payload cache cannot be reconstructed are released fail-closed on reopen.
Persistent stores take an active-runtime lease so two writable Runtime
instances cannot concurrently open the same database. File-backed SQLite
canonicalizes the database path for both the connection and lease. On systems
with `fcntl` and `O_NOFOLLOW`, its sidecar is opened no-follow, regular-file and
inode checked, and protected by `flock`; database/lease/journal/WAL/SHM files
are owner-only (`0600`). The fallback uses SQLite's
kernel-managed exclusive database lock. PostgreSQL derives its advisory-lock
key from the current database and schema. A clean close releases the lease and
permits a later reopen.
Checkpoint and image artifact payloads are explicit durable snapshot
exceptions.

Store transactions nest through savepoints, and repository helpers defer their
commits to the outer lifecycle transaction. Commit or savepoint-release failure
is followed by rollback, including restoration of an opted-in Object payload
cache snapshot. If rollback or savepoint cleanup also fails, the store is
poisoned and closed; every later operation fails closed. See
[Runtime Storage](storage.md) for the complete recovery and lease contract.

Audit and events are append-only. Checkpoint restore must not delete them.
Limited audit views select the latest matching records first and return that
window in chronological order, so GUI snapshots and per-process audit pages keep
showing new records as the log grows. Shell execution records an intent audit
record immediately before crossing into the shell provider; the result, timeout,
or resource-limit audit record uses the intent record as its parent and
correlation id.

## Module Map

```text
agent_libos/
  api/             CLI and GUI HTTP/SSE server entrypoints
  capability/      capability grant, revoke, check, and object handles
  config/          typed runtime, LLM, tool, memory, launcher, and script defaults
  human/           HumanObject query, approval, interrupt, and output primitives
  images/          built-in AgentImage definitions
  llm/             prompt, context, OpenAI-compatible client, executor, action parser
  memory/          typed Object Memory and MemoryView implementation
  models/          dataclass and enum models split by runtime domain
  modules/         trusted startup Runtime Module loader, registry, and core module
  primitives/      libOS primitives for filesystem, clock, shell, JSON-RPC, and MCP
  runtime/         composition, syscalls, scheduler, processes, events, checkpoints, audit
  skills/          Skill schema, strict loader, trust registry, and SkillManager
  substrate/       provider interfaces and local host-backed implementations
  storage/         runtime store backends
  tools/           tool base classes, ToolBroker, sandbox, and built-in tools
  utils/           shared validation, YAML loading, and helper utilities
benchmarks/        deterministic runtime-safety benchmark harness and fixtures
docs/              current implementation documentation
experiments/       benchmark entrypoints
gui/               Electron/React desktop console
images/            workspace AgentImage packages
modules/           workspace trusted Runtime Module packages
scripts/           real-model smoke and demo scripts
skills/            workspace standard Agent Skill packages
tests/             safety-boundary and regression tests
```
