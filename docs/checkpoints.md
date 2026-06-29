# Checkpoints

Checkpoints are capability-controlled durable snapshots of reconstructable
runtime state for one process subtree. They are a durable execution building
block, not a mechanism for rewinding the outside world.

## Captured State

A checkpoint captures scoped state needed to reconstruct the owner subtree:

- process rows and statuses,
- process working directories,
- Object Memory metadata and payloads for the subtree,
- process namespaces and object links,
- subtree capabilities,
- process tool tables,
- JIT candidates and registered process-local JIT tools,
- Skill registry rows needed by loaded Skills,
- loaded Skill records,
- mailbox delivery state,
- image definitions needed by the subtree,
- checkpoint-derived image artifacts needed by those image definitions,
- loaded startup Runtime Module ids and source hashes.

Transient `running` state is normalized to `runnable` at snapshot time. Forking
from a checkpoint also normalizes transient wait states such as waiting for an
event, tool, or human response back to `runnable`; the forked process must
re-enter those waits explicitly under its new identity.

## Append-Only Boundary

Restore never deletes:

- audit records,
- events,
- LLM call records,
- checkpoint records,
- human interaction history.

Restore itself appends new audit and event records.

Restore and fork require the current Python runtime to have already loaded the
same startup Runtime Module ids and source hashes captured in the checkpoint.
Checkpoint restore does not import Python modules, change module trust, restore
global Skill trust rows, or roll back the host module environment.

Host filesystem, shell, JSON-RPC/MCP remote calls, and provider effects are not
rolled back. Image registry rows are internal runtime metadata: snapshots capture
the image definitions needed by the checkpointed subtree and any referenced
checkpoint-derived image artifacts. Destructive restore re-upserts those captured
image rows so the restored subtree can run against its snapshotted image
definitions; it does not copy image-package source directories or provider-side
state.

Providers classify their own effects as:

- `irreversible`,
- `rollbackable`,
- `no_rollback_required`.

Restore reports provider-recorded effects in
`external_effects_since_checkpoint`, summarizes them in
`external_effect_summary`, and returns `restore_external_policy:
"report_only"`. In v1, `rollbackable` means the provider says a future
compensation layer could reason about the effect; restore still does not apply
external compensation.

## Capability Model

Checkpoint authority uses these resource forms:

- `checkpoint:process:<pid>`
- `checkpoint:<checkpoint_id>`
- `checkpoint:*`

Rights map to operations:

- `write`: create a checkpoint.
- `read`: list, inspect, diff, or replay diagnostics.
- `execute`: fork from a checkpoint.
- `admin`: destructive restore.

Creating a checkpoint does not automatically grant destructive restore
authority to the creator.

## Public Operations

LLM-facing tools:

- `create_checkpoint`
- `list_checkpoints`
- `inspect_checkpoint`
- `diff_checkpoint`
- `restore_checkpoint`
- `fork_checkpoint`
- `commit_checkpoint_to_image` for checkpoint-derived AgentImage commits

Default images expose low-risk create/list/inspect/diff tools. Restore and fork
tools are registered but require explicit tool visibility plus checkpoint
authority.

JIT syscalls:

- `checkpoint.create`
- `checkpoint.list`
- `checkpoint.inspect`
- `checkpoint.diff`
- `checkpoint.restore`
- `checkpoint.fork`
- `checkpoint.replay_to_event`
- `image.commit_checkpoint`

CLI commands:

```bash
uv run agent-libos --db .agent_libos.sqlite checkpoint create <pid> "before risky edit"
uv run agent-libos --db .agent_libos.sqlite checkpoint list --pid <pid>
uv run agent-libos --db .agent_libos.sqlite checkpoint inspect <checkpoint_id>
uv run agent-libos --db .agent_libos.sqlite checkpoint diff <checkpoint_id>
uv run agent-libos --db .agent_libos.sqlite checkpoint restore <checkpoint_id>
uv run agent-libos --db .agent_libos.sqlite checkpoint fork <checkpoint_id>
uv run agent-libos --db .agent_libos.sqlite checkpoint replay <checkpoint_id> <event_id>
```

Passing `--actor-pid` makes the CLI enforce that process's checkpoint
capabilities. Omitting it runs as an audited admin CLI actor.

## Restore

Restore applies at a runtime safe point and is scoped to the checkpoint owner
subtree. If the scheduler is actively running a quantum or still has active
futures, restore is rejected and the caller must retry after the runtime is
quiescent. Unrelated processes are not restored.

All post-checkpoint pending human requests for restored processes are cancelled.
Post-checkpoint mailbox entries are kept in history but marked as superseded by
restore so they are not delivered as unread by default.

After restored process rows are written, restore reconciles wait states against
the restored runtime facts. A process waiting on an already-terminal child or a
now-available matching message is made runnable. A process waiting on a human
request that restored as approved resumes; a restored `permission_request`
wait also becomes runnable so the permission path can re-check the current
capability state. Other resolved human waits become paused with an explanatory
status message. This keeps checkpoint state from preserving stale waits whose
blocking condition has already resolved in the restored snapshot.

Scoped Object Memory rows removed by restore run registered release finalizers
before their rows and payloads are deleted, so host resources such as PTY
sessions are not orphaned by a destructive restore.

If irreversible provider effects exist after the checkpoint, restore still
continues by default. The irreversible effects stay in append-only history and
in the restore report.

JSON-RPC endpoint and MCP server registry rows are host provider configuration
and are not captured or restored. Restored capabilities that reference a
missing endpoint or server fail closed until a host operator registers that
provider configuration explicitly.

## Commit To Image

A checkpoint can be committed into a new `AgentImage`, similar in spirit to a
Docker image commit but scoped to Agent libOS reconstructable runtime state.
The v1 commit captures only the checkpoint owner root process:

- Object Memory metadata and payloads reachable from that process,
- process-local namespace state,
- loaded Skill records and package rows,
- visible static tools and process-local JIT tool sources,
- process cwd and image context settings,
- required startup module summaries.

It does not copy the real filesystem, shell state, remote JSON-RPC/MCP state,
human UI output, network effects, resource budgets/usage, or any other
provider-side state. It also does not restore JSON-RPC endpoint registrations,
MCP server registrations, or global Skill trust rows; those are host registry
decisions, not image state. Provider effects remain append-only
`external_effects` records. Resource limits for a process booted from the
committed image must come from the caller that starts that process. Use an
image package `workspace/` seed when an image needs filesystem content at boot
time.

External capabilities in the checkpoint are converted into image
`required_capabilities` declarations. They are not restored as live authority
when the committed image is spawned or execed. Internal Object Memory
capabilities needed to read the baked objects are remapped into the new process.
Loaded startup module summaries are copied into image `required_modules`; the
committed image fails closed at boot unless those exact module ids and source
hashes are loaded in the current runtime.

CLI example:

```bash
uv run agent-libos --db .agent_libos.sqlite images commit <checkpoint_id> stateful-agent:v0 --name stateful-agent
uv run agent-libos --db .agent_libos.sqlite spawn --image stateful-agent:v0 --goal "use baked memory"
```

The model-visible commit path is the `commit_checkpoint_to_image` tool; the JIT
syscall path is `image.commit_checkpoint`. Actor-mode commits require `write`
on the target `image:<image_id>` and read authority on either the checkpoint or
the checkpointed process.

## Fork From Checkpoint

Fork creates a new isolated process subtree from checkpoint state. It remaps
pids, object ids, capability ids, namespace ids, and process-local tool records.
Fork is non-destructive for the global image registry: it restores only missing
image definitions, and restores checkpoint-derived image artifacts only for the
missing images it reintroduces. It never replaces an image id that already
exists. When actor capability checks are enabled, fork therefore requires both
`execute` on the checkpoint and `write` on each missing `image:<image_id>`
resource it would reintroduce.

The forked subtree must not gain authority wider than the checkpointed subtree
held. It does not share the original process private namespace or result
objects by reference. Checkpointed ObjectTask result objects are remapped to the
forked process-result owner boundary rather than pointing back at the original
task id.

Fork capability copying is checked against current capability state before rows
are remapped. Revoked or expired capabilities are not copied. If a current
restrictive `deny` or `ask` capability may overlap a checkpointed `allow`
capability, the overlapping right is dropped from the forked copy. This is
conservative by design: capability records do not encode exceptions, so a fork
must not recreate broad authority that current policy has narrowed since the
checkpoint was taken.

## Replay To Event

`replay_to_event` is diagnostic timeline replay. It reports event history from
a checkpoint to a target event. It does not rerun LLM calls, tools, syscalls, or
external side effects.

## Limits

Checkpoint defaults live in `CheckpointDefaults`:

- snapshot version,
- checkpoint list limit,
- payload capture limit,
- snapshot hard byte limit,
- diff preview size,
- auto high-risk checkpoint toggle.

Payload capture is bounded. A checkpoint can fail when reconstructable state is
larger than configured limits.
