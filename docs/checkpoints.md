# Checkpoints

Checkpoints are capability-controlled durable snapshots of reconstructable
runtime state for one process subtree. They are a durable execution building
block, not a mechanism for rewinding the outside world.

## Captured State

A checkpoint captures scoped state needed to reconstruct the owner subtree:

- process rows and statuses,
- process working directories,
- Object Memory metadata and payloads owned by the subtree,
- borrowed MemoryView root references and their types, without treating the
  referenced Object as subtree-owned state,
- process namespaces and object links,
- subtree capabilities,
- process tool tables,
- JIT candidates and registered process-local JIT tools,
- compatibility Skill registry rows associated with loaded Skills (never
  applied to the current global Skill registry by restore/fork),
- loaded Skill records,
- mailbox delivery state,
- image definitions needed by the subtree,
- checkpoint-derived image artifacts needed by those image definitions,
- loaded startup Runtime Module ids and source hashes.

Transient `running` state is normalized to `runnable` at snapshot time. Forking
from a checkpoint also normalizes transient wait states such as waiting for an
event, tool, or human response back to `runnable`; the forked process must
re-enter those waits explicitly under its new identity.

Checkpoint creation reads the scoped SQL rows and in-memory Object payloads and
writes the checkpoint row, owner's `checkpoint_head`, owner's initial
`checkpoint:<id>` `read` capability, creation event, and creation audit in one
store transaction. Any event/audit/capability sink failure therefore rolls the
entire create result back instead of leaving a checkpoint whose id was never
returned. A concurrent store transaction appears wholly before or wholly after
the captured snapshot; it cannot split an Object row from its payload. The
denormalized process capability index is rebuilt from the capability rows read
by that same snapshot, so those two representations cannot disagree.

Ownership defines the destructive scope. Process- and process-result-owned
Objects, plus scoped ObjectTask results, are captured as reconstructable state.
A root in a `MemoryView` is only a reference: checkpointing a borrower does not
claim ownership of the lender's Object, payload, namespace, or capability.
Legacy snapshots that mixed roots into `object_oids` are reinterpreted from the
captured owner fields before restore/fork.

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
global Skill trust rows, replace the global Skill registry, or roll back the
host module environment. Each restored/forked process continues to use the
immutable `package_snapshot` in its `loaded_skills` record. Historical
checkpoint `skills` rows remain readable compatibility data, not a host
registry mutation.

Host filesystem, shell, JSON-RPC/MCP remote calls, and provider effects are not
rolled back. Image registry rows are internal runtime metadata: snapshots capture
the image definitions needed by the checkpointed subtree and any referenced
checkpoint-derived image artifacts. Destructive restore re-upserts those captured
image rows so the restored subtree can run against its snapshotted image
definitions; it does not copy image-package source directories or provider-side
state.

Providers classify their successful effects as:

- `irreversible`,
- `rollbackable`,
- `no_rollback_required`,
- `unknown` for an outcome whose external state cannot be proven.

Filesystem mutations, clock operations, shell execution, and PTY spawn reserve
finite-use authority before entering their provider boundary.
`ProviderEffectNotStarted` restores that exact reservation only when it
certifies that the operation's first provider observation did not start and no
earlier information flow occurred. Clock sleep/asleep begins its intent before
the first `monotonic()` measurement; ordinary first-measurement failure and all
failures after that observation consume the use, and elapsed-time measurement
marks the effect as information flow. Timeout, cancellation, resource-limit,
ordinary provider exception, and post-effect classifier failure otherwise
cannot prove non-execution, so the use stays consumed and an `unknown` effect
remains visible in checkpoint reports. A tool/provider error therefore does not
imply that no outside-world effect occurred. Filesystem/clock/shell, human
output, PTY spawn/write/resize/close, and live JSON-RPC/MCP calls persist this
uncertainty before the provider boundary as a pending external-effect intent,
then conditionally finalize the same id. If any post-provider sink fails,
checkpoint diff/restore still reports that durable `unknown` row. Cleaning up a
failed PTY session publication does not remove its uncertain spawn history.

Restore reports provider-recorded effects in
`external_effects_since_checkpoint`, summarizes them in
`external_effect_summary`, and returns `restore_external_policy:
"report_only"`. The summary includes `by_state` counts and an explicit
`pending` count in addition to rollback class and provider/operation totals. In
v1, `rollbackable` means the provider says a future
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

Creating a checkpoint grants the checkpoint owner process `read` on that exact
new checkpoint. It does not automatically grant `execute` or destructive
`admin` restore authority.

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

Finite read authority follows the operation shape. Actor-mode `list` consumes
the selected `checkpoint:*` read immediately, or the selected
`checkpoint:process:<pid>` read when `--pid` is supplied. `inspect`, `diff`, and
`replay` instead reserve an exact `checkpoint:<id>` read, falling back to the
checkpoint owner's process-read authority, for the duration of the diagnostic;
they restore that reservation if the diagnostic fails and commit it once on
success. Admin CLI mode skips these process capability checks.

## Restore

Restore applies at a runtime safe point and is scoped to the checkpoint owner
subtree. If the scheduler is actively running a quantum or still has active
futures, restore is rejected and the caller must retry after the runtime is
quiescent. Unrelated processes are not restored.

After scheduler quiescence, restore acquires the shared registry lifecycle lock,
then the Object ownership boundary, then the store mutation lock. It holds the
ownership/store boundary continuously from the first preflight read through the
main-state commit, and keeps the registry lock through image/JIT reconciliation.
That fixed order matches Runtime Module load/unload and prevents registry/store
lock inversion. Host-side process, capability, Object Memory, mailbox, and
ObjectTask mutations therefore linearize wholly before or after restore; they
cannot slip between validation and row replacement. Capability rows are
filtered inside that boundary against current active/expiry/restrictive policy
state. A revoke that commits before restore's linearization point wins and is
not resurrected from the snapshot.

Restore has three explicit phases:

1. **Preflight** validates checkpoint authority, required Runtime Modules,
   image artifacts, JIT metadata, and the absence of active scoped ObjectTasks.
   Checkpoint `admin` and all changed-image rights are reserved as one
   deduplicated set. Restoring over an image id that is already registered
   requires `admin` on that exact `image:<image_id>` resource; reintroducing a
   missing image requires `write`. A preflight failure restores those exact
   reservations and makes no reconstructable-state changes.
2. **Commit** replaces scoped SQL rows and in-memory Object payloads in one
   transaction. Only owned Objects/namespaces are deleted or replaced; borrowed
   roots remain references to current lender state. The composite finite-use
   set is settled in this transaction; a commit failure rolls back both state
   and authority settlement.
3. **Post-commit reconciliation** atomically reconciles captured image cache,
   image rows, and artifact rows, then restores and prunes process-local JIT
   registries while still holding the lifecycle lock. It never applies captured
   global Skill rows. The lifecycle lock is released before running release
   finalizers for scoped Objects removed by the commit, because finalizers may
   call host code. These operations cannot undo the committed process-state
   transaction. The restore event and final restore audit are also post-commit
   observability sinks; their failure cannot undo that transaction either.

A successful commit returns `main_state_committed: true`. If every
post-commit phase succeeds, `status` is `restored`; otherwise `status` is
`restored_with_warnings` and `post_commit_failures` identifies each failed
reconciliation/event/audit phase and error. Each recordable failure appends a
`checkpoint.restore.post_commit_failure` audit record. Callers must not retry a
`restored_with_warnings` result as though the main restore had rolled back.

All post-checkpoint pending human requests for restored processes are cancelled.
Post-checkpoint mailbox entries are kept in history but marked as superseded by
restore so they are not delivered as unread by default.

ObjectTask rows are append-only execution history rather than reconstructable
worker state. Restore still refuses any active task in the affected process or
Object scope. If a scoped task reached a terminal state after the checkpoint,
restore keeps its row but changes the status to `superseded_by_restore`, clears
the now-invalid runner/result references, and stores the previous status,
runner, result, and error in the task's wait metadata. The restore response,
event, and audit list those task ids as `superseded_object_tasks`. A task that
was already terminal at checkpoint time keeps its ordinary status because its
referenced process/Object state was part of the captured scope. This boundary
uses the task's terminal `completed_at`, not `updated_at`: late notification
delivery may update the row after checkpoint creation without changing when
the task actually became terminal. Legacy rows that have no `completed_at`
fall back conservatively to `updated_at`.

After restored process rows are written, restore reconciles wait states against
the restored runtime facts. A process waiting on an already-terminal child or a
now-available matching message is made runnable. A process waiting on a human
request that restored as approved resumes; a restored `permission_request`
wait also becomes runnable so the permission path can re-check the current
capability state. Other resolved human waits become paused with an explanatory
status message. This keeps checkpoint state from preserving stale waits whose
blocking condition has already resolved in the restored snapshot.

Restored `llm_pending_actions` remain durable wait generations. Before the next
quantum, the executor synchronizes its in-memory human/child/message waiter with
the restored row and `resume_token`, so a pre-restore cached generation cannot
claim or clear it. Restore also assigns every restored process a fresh durable
LLM context generation inside the main transaction. Provider-side Responses
history is append-only and is not rolled back, so this generation change forces
the next LLM request to reset stateless instead of chaining to a response made
from post-checkpoint local state.

Scoped Object Memory rows removed by restore run registered release finalizers
after the main state commit. A finalizer failure is surfaced through the
post-commit warning contract above so operators can reconcile host resources
such as PTY sessions without confusing that failure with a rolled-back restore.

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

- Object Memory metadata and payloads both referenced by and owned/captured for
  that process (borrowed roots without captured payloads are excluded),
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
the checkpointed process. Replacing an existing target image requires `admin`
instead of `write`. Both finite decisions settle with artifact/manifest/event/
audit publication, so a failed commit does not burn either one-shot grant.

## Fork From Checkpoint

Fork creates a new isolated process subtree from checkpoint state. It remaps
pids, owned object ids, capability ids, namespace ids, ephemeral JIT tool ids,
and JIT candidate ids. Candidate Object payloads and loaded-Skill JIT mappings
are rewritten to those new identities, so unloading or replacing a forked tool
cannot retire the source process's registration.

Executable JIT handles/sources are prepared before publication. The missing
Image/artifact rows, all fork rows, Object payload cache entries, and any parent
child-budget reservation/charge then publish in one store transaction. On
failure the prepared handles and newly introduced images are discarded; the
scheduler cannot claim a fork root whose process-local JIT assets are only
partially installed.

Fork is non-destructive for the global image registry: it restores only missing
image definitions, and restores checkpoint-derived image artifacts only for the
missing images it reintroduces. It never replaces an image id that already
exists. When actor capability checks are enabled, fork therefore requires both
`execute` on the checkpoint and `write` on each missing `image:<image_id>`
resource it would reintroduce.

Fork also never replaces the global Skill registry. Forked processes consume
their captured `loaded_skills.package_snapshot`; checkpoint compatibility rows
are not upserted into host Skill state.

The forked subtree must not gain authority wider than the checkpointed subtree
held. It does not share the original process private namespace or result
objects by reference. Checkpointed ObjectTask result objects are remapped to the
forked process-result owner boundary rather than pointing back at the original
task id. A source capability with `uses_remaining` set is never copied into the
fork, even when it still has uses left; cloning a finite grant would multiply
one-shot authority.

`EXTERNAL_REF` Objects are host-handle descriptions, not clonable runtime
resources. Fork drops both owned and borrowed `EXTERNAL_REF` roots and filters
their object capabilities; it never reconnects or aliases the source PTY or
other host handle. Destructive restore likewise mutates only subtree-owned
Objects and does not roll back a borrowed Object or its lender's capability.

Fork first performs non-consuming preparation checks: the requested parent must
exist and be non-terminal, cross-subject parent attachment needs `admin` on the
parent's checkpoint-process resource, the actor needs `execute` on the source
checkpoint, and every missing snapshot image needs `write` on its exact image
resource. All checks run again inside the same store transaction that publishes
the fork, and finite-use actor authority is consumed only there. A concurrent
revoke, newly terminal parent, missing image permission, or later transaction
failure publishes no fork/image/object/process rows and rolls back that
transaction's one-shot consumption. Only a successful publication spends the
grant.

Snapshot capability copying is also revalidated at the publication point, so a
revoke committed before it wins. Revoked, expired, and finite-use capabilities
are not copied. If a current
restrictive `deny` or `ask` capability may overlap a checkpointed `allow`
capability, the overlapping right is dropped from the forked copy. This is
conservative by design: capability records do not encode exceptions, so a fork
must not recreate broad authority that current policy has narrowed since the
checkpoint was taken.

After main-state publication, fork event/audit emission is observability rather
than rollbackable state. Success returns `status: forked` and
`main_state_committed: true`. If either post-commit sink fails, the fork remains
published and the result instead uses `status: forked_with_warnings` with
`post_commit_failures`; retrying it as an uncommitted fork would duplicate the
subtree.

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
- diff preview size.

Checkpoint list callers may request only positive integer limits no larger
than `CheckpointDefaults.list_limit`; `0`, negative values, booleans, and larger
values are rejected rather than being passed through as unbounded SQL limits.

There is no automatic high-risk checkpoint setting. The former
`auto_high_risk_checkpoint` config field described an unimplemented idea and is
now rejected by strict config loading. Callers that need a safety snapshot must
create it explicitly before the risky operation.

Payload capture is bounded. A checkpoint can fail when reconstructable state is
larger than configured limits.
