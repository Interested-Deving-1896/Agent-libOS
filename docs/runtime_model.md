# Runtime Model

Agent libOS models work as `AgentProcess` instances. A process has identity,
status, a goal object, a memory view, a process-local working directory, a
complete callable tool table, a separate model tool projection, loaded Skills,
capabilities, children, a Task Authority Manifest, message queue state, an
`llm_profile_id`, and resource budgets.

The paper frames this process model as the substrate for self-evolving agents:
a process can change visible tools, activate Skills, register process-local JIT
tools, register or exec AgentImages, fork children, and fork from checkpoints,
while resource authority remains separate in Capability.

## Process Lifecycle

The current lifecycle includes:

- `created`: process row exists but has not started running.
- `runnable`: scheduler may run the process.
- `running`: a quantum is currently executing.
- `waiting_human`: the process is blocked on a human question or approval.
- `waiting_event`: the process is blocked on a child, message, or event.
- `waiting_tool`: reserved waiting state for tool-level blocking.
- `paused` / `suspended`: the process is not selected for normal execution.
- `exited`: completed successfully.
- `failed`: completed with failure.
- `killed`: terminated by signal or runtime decision.

Terminal statuses are `exited`, `failed`, and `killed`.

## Images And Tool Tables

An `AgentImage` defines the default process prompt, tool table, default Skills,
prompt mode, context policy, safety profile, declared required capabilities,
declared required startup modules, an optional default LLM profile, and
optional boot metadata. Fresh images boot from their manifest.
Checkpoint-commit images boot from an immutable internal runtime artifact
derived from one checkpoint root process. Image-package images boot from an
immutable directory-package artifact created from `IMAGE.yaml`, `prompt.md`,
optional `tools/`, optional `resources/`, and optional `workspace/`.

Image registration and replacement keep the in-memory image cache, durable
manifest, event/audit records, and any newly inserted package or checkpoint
artifact in one transaction. A failure after artifact insertion therefore
restores the previous image (for replacement) or removes the new image and
artifact (for registration/commit); a caller never observes a manifest whose
registration result was reported as failed. A registry-wide reentrant lock
covers the cache/store critical section, and `replace=false` is revalidated
inside it. Two concurrent registrations for one id cannot both win, and one
caller's rollback cannot delete another caller's committed cache entry.

The registry owns deep snapshots of image definitions. Mutating the caller's
registration object, a returned registration result, or an object returned by
`Runtime.get_image()` does not mutate the cached/durable definition. Booting a
checkpoint-committed image restores the process's captured
`loaded_skills.package_snapshot`, but does not replace the current global Skill
or Image registry with historical nested metadata.

`prompt_mode` controls prompt composition. `image_only` uses the image prompt as
the system prompt and gives the model only materialized task context; this is
the default for custom images and image packages. `minimal_runtime` adds a
short factual runtime note and state sections. `libos_default` preserves the
native Agent libOS planner envelope and fallback JSON instructions used by the
built-in images.

Root process spawn never grants image `required_capabilities` in the default
`manifest_required` mode. Requirements are copied into the Host-authored
Task Authority Manifest and reported as satisfied or unmet. Only the
manifest's `authorized_capabilities` compile into authority. The explicit
`legacy_image_grants` compatibility mode retains the older bootstrap behavior.
`exec_process`, checkpoint-commit image boot, and image-package boot never
grant requirement declarations automatically.
Image `required_modules` are always startup prerequisites only: spawn and exec
fail unless each declared module id is already loaded with the declared
`source_sha256`.

At process creation time, the runtime resolves only the image's explicit
`default_tools` into the process tool table. No lifecycle, Object Memory, or
other builtin tool is implicitly added. A process can call only tools in that
table, but visible tools still fail at primitive use if resource authority is
missing. If an image wants LLM-facing `process_exit`, Object Memory, filesystem,
shell, or other builtin access, it must list that tool explicitly. Internal
runtime paths such as JIT syscalls may still call primitives directly through
their syscall session without exposing the corresponding builtin tool to the
model.

An image with `metadata.lazy_tool_groups=true` initially projects only the
stable discovery/core subset into LLM schemas. `discover_tool_groups` and
`activate_tool_group` expand the durable model projection from the already
authorized image tool table. Host calls and primitive capability enforcement
continue to use the complete table; activation cannot grant authority.

LLM selection is host-controlled and process-local. A process stores only an
`llm_profile_id`; the host Runtime resolves that id to a configured
OpenAI-compatible profile at LLM-call time. Root spawn uses an explicit host
profile, then the image default, then `config.llm.default_profile_id`. Fork and
fresh child creation inherit the parent profile by default. Exec keeps the
current profile unless the host explicitly overrides it. Model-facing process
tools do not expose LLM profile switching in v1. Only the configured default
profile inherits legacy `OPENAI_*` provider and model environment variables;
other named profiles require explicit host profile fields for non-default
routing.

The default OpenAI posture is stateless and privacy-preserving:
`llm.store=false` and `responses_previous_response_id=false`. Opt-in Responses
chaining additionally requires full local I/O persistence, the official
Responses request path, the same profile/scope fingerprint, the same non-secret
provider-chain fingerprint, and a complete one-to-one durable output for every
unique function `call_id` in the immediately preceding response. The provider
fingerprint is a credential-keyed HMAC over the model, normalized official
endpoint, API mode, API-key environment name, and organization/project tenant;
the credential itself is never persisted. This keeps a same-identity chain
stable across restarts while a model, credential, endpoint, or tenant change
forces a reset. Eligible outputs, including completed parallel batches and
waits resumed after reopen, are sent as native `function_call_output` items.
Any missing, extra, redacted, conflicting, legacy-ambiguous, or partial output,
or a changed image/tool/Skill/context generation, resets to stateless/plain
context instead of guessing provider state. Context compaction advances the
durable generation before payload replacement; checkpoint restore also advances
it so a local rollback cannot chain to a response produced from post-checkpoint
state.

LLM-selected blocking actions use durable wait generations. Each row has a
unique `resume_token` plus the causal LLM and Tool operation ids; one executor
CASes `pending -> resuming` before crossing
the resumed primitive boundary and CASes the same generation to `completed`
afterward. Reblocking writes a new token, closing stale-worker ABA. Once the
claim succeeds, any dispatch, output-persistence, or completion exception
immediately fails the process, retains the non-replayable durable state, and
audits `llm.pending_action_resume_interrupted`; reopening an already-`resuming`
row follows the same fail-closed rule rather than replaying a possibly completed
external effect.

The Explainable Operations lifecycle mirrors this state machine. The LLM and
Tool rows become `waiting` before control returns to the scheduler. Resume
reactivates those exact rows even though the concrete retry may get a new
`call_id`. A runtime reopen preserves waiting rows but marks any orphaned
`running` row `interrupted`. Human terminal evidence is attached to the waiting
Tool operation without prematurely changing it to running. See
[explainable_operations.md](explainable_operations.md).

`jit_tool_exposure` controls how JIT tools appear to the LLM. `direct` exposes
each visible JIT as its own OpenAI tool. `multiplexed` exposes one stable
`run_jit_tool` protocol tool and maps it back to the real process-local JIT
before execution. Multiplexed mode hides individual JIT names from runtime
tool sections and event context; image prompts remain responsible for listing
any JIT catalog the model should know.

A checkpoint-commit image remaps baked Object Memory, process-local JIT tools,
loaded Skill package snapshots, and cwd into the new process. It also carries
the checkpoint's loaded startup module summaries as `required_modules`. It does
not package or restore filesystem, shell, JSON-RPC endpoints, global Skill
trust, human, network, or provider side effects.

An image-package boot materializes the package `workspace/` seed into a private
per-process directory under `agent_outputs/image_workspaces/`, sets the process
cwd from the package manifest, and grants only the manifest-declared
`workspace.grants` for that private copy. Package JIT tools live under
`tools/jit-tools.json` and `tools/scripts/*.ts`; they are registered as
process-local ephemeral tools and are not copied into the workspace. Package
artifacts persist only declared package content: `IMAGE.yaml`, the referenced
prompt, declared `workspace/` content, referenced `tools/` JIT files, and
`resources/`. Cache, VCS, likely secret, and platform-unsafe paths are rejected.
Failed package boot or exec removes the private workspace, unpublished JIT
tool rows and process aliases, candidate source rows, and candidate Object
Memory descriptors before returning failure.

## Working Directory

Each process has its own workspace-relative working directory. Relative
filesystem paths and shell subprocess cwd resolve from that process cwd. The
runtime host process does not `chdir` into launched workspaces.

Changing a process cwd requires `read` on the selected filesystem directory.
An explicit cwd supplied to spawn, fork, or PTY creation is checked through the
same filesystem directory primitive after the higher-level spawn/image or
shell authority gates. The directory `state()` observation therefore runs
under a structured filesystem intent rather than acting as an unauthorized
existence oracle. Finite directory-read authority is consumed only after an
observation; an ambiguous provider failure leaves unknown effect evidence.

The CLI command:

```bash
uv run agent-libos --db .agent_libos.sqlite cd <pid> src
```

updates one process working directory and leaves other processes unchanged.

## Object-Bound PTY Sessions

The trusted `modules/pty` runtime module can add an interactive PTY surface.
`pty_create` starts the host PTY through the shell primitive's authorization
path and returns a mutable Object Memory `EXTERNAL_REF` object id. The object
payload records descriptive metadata such as argv, cwd, backend, dimensions,
and creation time, but authorization for later interaction comes only from the
current Object capability graph and the in-memory PTY registry.

`pty_read` requires object `read`; `pty_write` requires object `write` and the
original session owner pid; `pty_resize` requires object `write`; `pty_close`
requires object `delete`. Closing the session releases the object and revokes
related object capabilities. If the object is released by a lifetime scope,
process-owned memory cleanup, direct trusted delete, or runtime shutdown, the
module-bound Object release or shutdown finalizer closes the underlying PTY as
the object's RAII resource.

Finite object write/delete decisions for `pty_write`, `pty_resize`, and
`pty_close` are reserved before the host call. A certified not-started result
restores that exact reservation only when no earlier provider observation in
the operation flowed information; ordinary or ambiguous failures commit the
use and retain pending/unknown evidence. When the child exits on its own, the
monitor writes a close intent before reading the exit code and closing the
handle. Once the exit-code read succeeds, even a later not-started close cannot
abandon that information-flow intent.

PTY spawn, write, resize, and close are external-effect operations. Each writes
a structured pending intent before its provider boundary and conditionally
finalizes the same effect id afterward; event/audit/finalization failure leaves
the row pending and unknown. A spawned host session whose Object publication
later fails is cleaned up but remains an `unknown` spawn effect with
failure-phase and cleanup metadata. Unsupported or failing post-operation
classification finalizes an unknown fallback rather than erasing the effect.
The local provider classifies write/close as irreversible and resize as
rollbackable-but-not-applied; checkpoint restore does not compensate either.

PTY sessions are not checkpointed or persisted as reconnectable host handles.
A checkpoint or committed image may contain an `EXTERNAL_REF` row only as
descriptive metadata; it cannot rewind or recreate the provider resource.
Checkpoint fork drops owned and borrowed `EXTERNAL_REF` roots and object
capabilities rather than aliasing the source terminal. Reopening the runtime
releases stale PTY objects rather than trying to reconnect a host process.

## External Effect Ledger

Filesystem, clock, shell, human output/terminal I/O, PTY, JSON-RPC, and live MCP
primitives close the crash gap around provider calls with a durable
external-effect intent.
Immediately before the provider boundary they insert an `unknown` record with
structured `effect_state: pending`. A classified success or ambiguous provider
exception CASes that same `effect_id` to `finalized`, matching its pid,
provider, operation, and target. Repeated, stale, or cross-boundary finalization
fails closed instead of adding another final record or altering unrelated
evidence.

Transaction outcome and rollback support are separate axes. A confirmed
provider completion is `transaction_state: committed` even when rollback
support remains `unknown`; only an explicit unknown provider outcome is
`transaction_state: unknown` and propagates `unknown` to its operation tree.

`ProviderEffectNotStarted` is the only provider result that can prove its call
boundary was not crossed. The primitive conditionally deletes a still-pending
intent only when no earlier provider observation in the composite operation
flowed information. Filesystem/clock/shell and PTY spawn restore any exact
finite-use reservation and abandon the intent in one transaction. If an earlier
filesystem state read or MCP live validation succeeded, a later not-started
mutation/tool call finalizes an information-flow-only record. After the provider
may have run, a failure in capability commit, resource/event/audit handling,
classification, or final effect persistence leaves the pending `unknown` record
in place. Checkpoint diff/restore and the runtime-safety benchmark consume that
row conservatively, so a process crash cannot turn an uncertain external effect
into “no effect recorded.”

Terminal human reads and automatic prompt/decision writes follow the same
protocol. Their effect context contains request id, purpose, byte/character
counts, and hashes only; raw prompts, answers, and provider exception text are
not persisted. A successful human interaction is not replayed when later
event/audit/classification settlement fails: the request decision commits and
the pending intent remains the reconciliation evidence. A Human provider that
certifies `ProviderEffectNotStarted` instead abandons the intent and restores
retryable request/finite-authority state.

Startup reconciliation is isolated per pending intent. If one provider's
reconciliation hook raises, that intent remains explicitly `unknown` and the
runtime continues opening and reconciling other providers; the exception text
is not persisted.

For JSON-RPC and Streamable HTTP MCP, the pending intent and all finite remote
authority reservations are durable before non-local DNS resolution. A
successful DNS lookup, or an ordinary DNS failure after host observation, is
already information flow and commits the reservations. Consequently a later
transport-level `ProviderEffectNotStarted` finalizes an information-flow-only
unknown outcome instead of abandoning the intent. Local HTTP fast paths and
stdio have no DNS observation, so a certified failure at their first provider
boundary can still restore and abandon atomically.

Clock sleep is one composite observed operation. Synchronous `sleep` and async
`asleep` persist the intent before their first provider `monotonic()` call and
mark it `information_flow=true`, including on success, because the returned
elapsed time comes from provider observations. Only
`ProviderEffectNotStarted` at that first measurement can restore a finite-use
reservation and abandon the intent. Any ordinary first-measurement exception,
or any later sleep, cancellation, or second-measurement failure, consumes the
reservation and finalizes the same id as `unknown`.

## Scheduler

The scheduler is thread-backed. It starts one worker task per runnable process
up to `config.scheduler.max_workers`, and each task advances only that process
until it blocks, exits, fails, or the shared quantum budget is exhausted. Public
async APIs remain available for event-loop hosts, but they are wrappers around
the same scheduler and do not mean process quanta are serialized on one asyncio
loop.

The high-level synchronous entrypoint is:

```python
results = runtime.run_until_idle(max_quanta=10)
```

Event-loop hosts should use:

```python
results = await runtime.arun_until_idle(max_quanta=10)
```

By default it:

1. runs runnable processes,
2. processes pending human terminal messages when work is blocked on human I/O,
3. delivers process-message notices at tool boundaries,
4. wakes resumed processes,
5. stops when no runnable or human-resumable work remains, or when the quantum
   budget is exhausted.

`max_quanta` is a global budget across all process workers, not a per-process
limit. A bounded run may briefly reserve extra dependency quanta when a running
process is already waiting on a runnable child or message dependency and the
main worker pool is full; that keeps parent/child waits from deadlocking behind
the nominal budget. After budget exhaustion, `config.scheduler.drain_window_s`
gives already-running workers a short chance to finish before unfinished quanta
are cancelled or detached.

The scheduler serializes top-level `run_until_idle`, `run_pid_until_idle`, and
single-step invocations for one `Runtime` instance, so two host calls cannot
re-enter the same runnable process concurrently. Individual process claims are
also status-checked at the store boundary before a quantum changes a process
from `runnable` to `running`.

Single-step APIs remain available for tests and debugging:

```python
result = runtime.run_next_process_once()
result = runtime.run_process_once(pid)

result = await runtime.arun_next_process_once()
result = await runtime.arun_process_once(pid)
```

For debugging pending approval states, disable human queue processing:

```python
results = await runtime.arun_until_idle(max_quanta=1, process_human_queue=False)
```

`Runtime.shutdown()` first asks the scheduler to cancel and join tracked worker
futures for up to `config.scheduler.shutdown_join_timeout_s`, then asks
ObjectTask runners to drain their tool executor for up to
`config.object_tasks.shutdown_join_timeout_s`. If a synchronous quantum or
ObjectTask tool thread cannot stop safely, shutdown reports `ok: false` with
`scheduler_stopped: false` or `object_tasks_stopped: false` and leaves the
runtime store open rather than closing it underneath a live worker. Once the
worker finishes, a later shutdown can complete normal resource cleanup.
Persistent stores also take an active-runtime lease: SQLite uses a secure
sidecar `flock` where available or an exclusive database lock as fallback, and
PostgreSQL uses a database/schema-scoped session advisory lock. Another
writable Runtime cannot open the same database until the active Runtime closes
and releases the lease.

The GUI adds a service-level drain around this contract. It stops background
scheduling, rejects new runtime users, waits for tracked request handlers, and
only then calls `Runtime.shutdown()`. A timeout returns failure and reopens the
service lifecycle gate so shutdown can be retried; it does not mark the service
closed or close the store underneath live work.

## Resource Budgets

Resource limits are runtime constraints, not Capabilities. A process may have a
`ResourceBudget` covering tool calls, child processes, runtime seconds, LLM
calls and tokens, context materialization tokens, subprocess wall/CPU/RSS usage,
external filesystem bytes, JSON-RPC bytes, MCP bytes, and Deno syscalls.
Observed consumption is stored as `ResourceUsage` on the process row.

Discrete counters and byte/token quantities must be non-negative integers:
tool/child/LLM call counts, token counts, context counts, Deno syscall counts,
filesystem/JSON-RPC/MCP byte counts, and subprocess peak bytes reject floats
and booleans. Runtime duration and subprocess wall/CPU seconds are continuous,
finite non-negative numbers and may be fractional. This distinction is checked
when budgets/usages are constructed and again at the resource manager boundary.

Every charge applies to the acting process and its parent chain, so a parent can
bound an entire child tree. The complete child-to-ancestor usage update,
reservation consumption, resource event, and audit row commit in one store
transaction; a failure at any point leaves none of that charge published. If an
overage kills a process subtree, terminal Human/Object-task/finalizer callbacks
run only after the store transaction and lock are released. Fork and spawn may
request a child budget, but it must fit within the parent's remaining budget.
`exec_process` keeps the same pid and does not reset usage or increase budget.
Checkpoint restore replays recorded process rows, including their resource
state, for the restored processes.
Checkpoint-committed images do not store or restore resource budgets or usage;
only the caller that starts the process may set launch-time resource limits.

LLM token usage is charged after provider completion using provider-reported
usage. If a token budget exists and the provider does not return billable usage,
returns booleans/strings/negative values, or reports a total smaller than its
prompt-plus-completion components, the LLM action fails closed. When an LLM
completion pushes usage over budget, the call record is retained but
model-selected tools are not dispatched.
Context materialization has both a per-call cap
(`max_context_materialization_tokens`) and a separate cumulative budget
(`max_context_materialization_total_tokens`). The cumulative context token
budget is charged when Object Memory materializes prompt context and is
accounted independently from provider-reported LLM tokens. In the LLM executor,
the final rendered `llm_context:<pid>` prompt context is the charged unit;
source object materialization for delta capture does not double-charge the same
quantum, and over-budget rendered context fails closed before the model call.

Shell and Deno subprocesses are run through provider-level monitors. The
default local provider uses cross-platform process-tree sampling to enforce wall
time, CPU time, and peak RSS budgets, then records metrics and audits limit
exceedance. Deno additionally runs behind a host-lifetime supervisor (a POSIX
death pipe or Windows `KILL_ON_JOB_CLOSE` Job Object), so a hard host exit does
not orphan untrusted JIT code. In-process Python primitives are not hard CPU/RSS isolated; they
remain bounded by call count, wall time, byte limits, and primitive-specific
caps.

## Human Queue

Human interaction is modeled as runtime objects, not raw prompt text.

- `ask_human` creates a blocking question.
- `request_permission` requires human write authority, creates a blocking scoped
  policy request with canonical resource, risk, resource scope, lease shape, and
  constraints shown to the human, then returns the final policy decision. Model
  requests cannot ask for broad high-risk grants such as `capability:*`
  privileged rights, `shell:*` execute, or root/global filesystem write such as
  `filesystem:/:*`; workspace write remains a human-approvable scope.
- `human_output` writes through the HumanObject primitive and provider. It
  commits `delivered` request state, audit, event, and a structured pending
  external-effect intent in one transaction before calling the provider. A
  provider exception is finalized as unknown when possible and records only
  `provider_error_type`, never exception text; if delivery succeeds but
  classification/final persistence fails, the pending row remains and the call
  is not retried. Thus output is at-most-once: no post-provider failure can
  leave a replayable request or restore already committed one-shot authority.
- Per-use approvals can create one-shot capabilities. Side-effectful primitives
  reserve the use before commit, restore it if a pre-commit failure aborts the
  operation, and leave it consumed once the operation crosses its commit or
  provider boundary.

If a primitive or human tool blocks on human approval, the process enters
`waiting_human`. Human requests are terminally decided once: only pending
requests can be approved or rejected. The runtime can process human terminal
messages, update the request, wake the process, and resume the original
operation. Rejection returns a normal failure to the process instead of crashing
the runtime, except `request_permission` rejection returns a structured
`rejected` decision after installing the selected deny policy.
Terminal queue selection claims the oldest pending request in one serialized
critical section, but blocking provider input/output runs outside that lock.
Concurrent drains therefore cannot deliver one output twice or install two
automatic permission policies from the same pending request, while process
exit/cancel can still cancel the claim without waiting for user input. A late
answer rechecks the durable pending state and is discarded after cancellation.

Permission decisions must include a JSON boolean `approved` consistent with the
terminal status and one explicit policy: `always_allow`, `always_deny`, or
`ask_each_time`. Approval cannot install `always_deny`; rejection cannot install
`always_allow`. `allow_once` remains a separate capability lease/API shape and
is not a terminal permission-response policy. An approved `question` must carry
a non-empty string `answer`; values are not coerced from numbers, objects, or
missing fields.

Automatic terminal policy belongs to one host run invocation. It is stored in
an immutable `ContextVar`, copied into scheduler workers, and captured by a JIT
syscall session, so concurrent runs cannot overwrite one another's human,
auto-policy, or answer. Resolving one of several blocking requests leaves the
process in `waiting_human` until no blocking request remains. Process exit,
failure, kill, or terminal cancellation cancels all still-pending requests;
terminal processes cannot create or receive new human decisions.

## Process Messages And IPC

Each process has a durable message queue. Messages include:

- sender and recipient pid,
- `kind` such as `normal` or `interrupt`,
- channel,
- correlation id,
- reply target,
- subject and body,
- structured payload,
- delivery and acknowledgement state.

Processes can send messages to themselves, their parent, or direct children.
Receivers use `read_process_messages` for non-blocking reads or
`receive_process_messages` to block until matching unread messages arrive.
Filters can match kind, sender, channel, correlation id, reply target, and exact
message ids. An explicit empty message-id filter matches no messages; it never
means "all messages." Read limits bound both returned messages and
acknowledgement; a blocking receive must use a positive limit and a non-empty
explicit id filter because zero-size receive windows cannot ever produce a
message.
An empty blocking read registers its wait atomically with message posting, so a
matching concurrent post either satisfies the read or wakes the registered
process; it cannot disappear between the mailbox query and wait-state update.
Recipient terminal-state recheck, message insertion, event/audit evidence, and
matching waiter wakeup commit in the same store transaction. An evidence sink
failure therefore leaves neither an orphan unread message nor a false wakeup.

Interrupt messages preempt before non-message tool calls until read. Normal
messages notify after a tool call and do not block the current action.

ObjectTask completion and waiting notices use the same queue. By default they
arrive on channel `object-task` from sender `object_task:<task_id>`. A process
can block with `receive_process_messages(channel="object-task")` and will be
woken by matching task notifications. If the selected notification process has
already exited, the task records `undelivered_terminal`; task success is not
converted into failure.

ObjectTask owner-watch notices also use this queue. They are addressed to the
runner process on `object-task-owner` by default and can resume a task that is
blocked in `receive_process_messages`; the notice contains event metadata and
object ids, not Object Memory read authority. The same ObjectTask resume hook
also observes ordinary process messages delivered to a waiting runner, and
child-process termination can resume a runner blocked in `wait_child_process`.
ObjectTask runner processes are host-managed and skipped by the LLM scheduler;
auto-resume is limited to tools with explicitly safe replay semantics, currently
`receive_process_messages` for message waits and `wait_child_process` for child
process waits.

CLI examples:

```bash
uv run agent-libos --db .agent_libos.sqlite message <pid> "Please inspect the result"
uv run agent-libos --db .agent_libos.sqlite interrupt <pid> "Stop and read this first"
```

## Fork, Spawn, Exec, Wait, Signal

`fork_child_process` creates a direct child with an attenuated parent
`MemoryView`. It can inherit selected capabilities only if the parent already
holds them.

`spawn_child_process` creates a fresh direct child with a new process namespace
and goal-only memory. It does not inherit parent-activated Skills or broad
external authority by default.

`exec_process` replaces the current image and tool table without changing pid.
It never grants the target image's declared required capabilities
automatically. Existing external capabilities are preserved only when explicitly
requested; otherwise exec shrinks external authority.
The core `ProcessManager.exec` transition commits the process image row,
capability shrink, exec event, and exec audit atomically. Higher-level
`Runtime.exec_process` then configures the target tool table, package/checkpoint
state, and default Skills. A later boot-phase failure cleans package state and
restores the captured process/Object/capability/tool state in a compensating
transaction, then records `image.boot.failed`; those phases are not one SQL
transaction. Hosts that call the embedded API concurrently must therefore
provide Runtime-level serialization if intermediate reads are unacceptable.

`wait_child_process` blocks the parent in `waiting_event`. Child exit wakes the
parent and resumes the original wait action without asking the model for a new
action. Terminal child state, budget release, exit evidence, and parent wakeup
commit atomically, so an evidence failure leaves the child retryable rather than
terminal with a stranded parent.

Signals can pause, resume, cancel, interrupt, or terminate direct children.

## Process Exit

`process_exit` marks a process as `exited` or `failed` and can attach a final
Object Memory result. Process-owned memory is released on exit unless retained
as the process result. Cleanup follows explicit Object Memory owner fields, not
the object's creator provenance, and release revokes stale object capabilities.
The terminal row, child-budget release, exit evidence, and parent wake commit
together. Object/host finalizers and ObjectTask terminal notification run after
that commit because their provider cleanup cannot be rolled back; a later
cleanup error does not make the terminal transition uncommitted.

When a Deno JIT tool calls `process.exit` or `process.exec`, the syscall records
a deferred lifecycle change. The runtime applies that change only after the JIT
tool returns its normal tool result.
