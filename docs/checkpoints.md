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
- JSON-RPC endpoint definitions referenced by subtree capabilities,
- mailbox delivery state,
- image definitions needed by the subtree.

Transient `running` state is normalized to `runnable` at snapshot time.

## Append-Only Boundary

Restore never deletes:

- audit records,
- events,
- LLM call records,
- checkpoint records,
- human interaction history.

Restore itself appends new audit and event records.

External filesystem, shell, image, JSON-RPC remote calls, and provider effects
are not rolled back. Providers classify their own effects as:

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
subtree. Unrelated processes are not restored.

Post-checkpoint pending human requests for restored processes are cancelled when
they conflict with restored state. Post-checkpoint mailbox entries are kept in
history but marked as superseded by restore so they are not delivered as unread
by default.

The current tool call's result object can still be appended after restore so
the process receives a coherent action result.

If irreversible provider effects exist after the checkpoint, restore still
continues by default. The irreversible effects stay in append-only history and
in the restore report.

JSON-RPC endpoint registry rows captured by the snapshot are restored by
upsert. Restore and fork do not delete unrelated endpoint registry state.

## Fork From Checkpoint

Fork creates a new isolated process subtree from checkpoint state. It remaps
pids, object ids, capability ids, namespace ids, and process-local tool records.

The forked subtree must not gain authority wider than the checkpointed subtree
held. It does not share the original process private namespace or result
objects by reference.

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
