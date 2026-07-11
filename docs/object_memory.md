# Object Memory

Object Memory is typed, capability-controlled runtime memory for agent state.
It is not a plain key/value store and object names are not capabilities.

## Objects

Objects have:

- an object id (`oid`),
- type,
- namespace,
- optional namespace-local name,
- version,
- metadata,
- payload,
- creator provenance (`created_by`),
- explicit owner (`owner_kind`, `owner_id`) and lifecycle state,
- object capabilities.

Object handles carry object-specific rights such as `read`, `write`,
`materialize`, `link`, `diff`, or `delete`.

`created_by` is provenance and does not drive cleanup. Runtime cleanup uses the
explicit owner pair. Ordinary process-created objects start as
`owner_kind=process`, `owner_id=<pid>`. A final result is retained by
transferring it to `owner_kind=process_result`, and ObjectTask results are
transferred to `owner_kind=object_task`.

Object release is centralized in `ObjectMemoryManager`. Releasing an object
marks the row as `released`, removes its runtime payload, deletes links that
touch it, and revokes stale `object:<oid>` capabilities. Released objects are
not returned by oid lookup, name lookup, namespace listing, or materialization;
their namespace-local names can be reused by new live objects.

Ownership changes are lifecycle changes and increment the Object version.
Create, update, append, transfer, and trusted delete all acquire the Object
Memory ownership lock before entering the store transaction. Updates,
transfers, and deletes condition their row write on `lifecycle_state=live` plus
the captured owner and version; a lost conditional update cannot report
success, overwrite a concurrent owner, or revive a released Object. Owner
cleanup enumerates candidates but deletes each one only if its `owner_kind`,
`owner_id`, and version still match the captured tuple. Transfer increments the
version and publishes the whole selected batch plus its audit row in one
transaction; a conditional failure transfers none of the batch. Therefore a
release racing an ownership transfer cannot delete the new owner's Object,
including an A-to-B-to-A (ABA) cycle: returning to the same textual owner does
not restore the old version.

Trusted delete holds the ownership lock while it snapshots and conditionally
checks the Object. Host-resource finalizers then run outside the SQL transaction
so a provider cleanup such as PTY close can durably write its own pending effect
intent before crossing the host boundary. A finalizer failure leaves relational
release state untouched. After finalization, delete opens a store transaction,
rechecks the exact LIVE/owner/version tuple, and commits the Object release,
object-capability revocation, and delete audit together. A later relational or
audit failure rolls back the Object row, capabilities, and in-memory payload;
it cannot undo an already completed host finalizer, whose effect ledger remains
the reconciliation evidence.

## Namespaces

Object names are local to a namespace. Runtime code that omits `namespace`
uses the caller process namespace:

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

Each process gets its own default namespace at spawn/fork time. A bare name
such as `notes` can exist independently in two process namespaces.

Explicit namespaces are directory-like scopes:

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

Namespace capabilities gate listing, lookup, and creation. They do not replace
object capabilities. Reading `project/research/notes` requires namespace read
authority and object read authority.

## Name Resolution

A name is not itself authority. Resolution requires:

1. namespace rights for the directory-like lookup,
2. object rights for the requested operation.

This prevents a process from using a guessed object id or shared name to bypass
capabilities.

One-shot namespace grants are consumed only after a successful namespace
operation. For example, an `allow_once` `object_namespace:<ns>` `read` grant can
complete one named lookup or namespace listing, then later lookups must be
authorized again. Failed validation or missing objects do not burn the grant.

Namespace listing also consumes the finite visibility authority actually used
to construct its result. In one store transaction it settles the parent
namespace read plus each returned object's `object:<oid> read` decision and
each returned child namespace read decision, deduplicating repeated capability
ids. Thus an object visible through a one-use object-read grant appears in the
first successful listing and is absent from the next; listing cannot use a
temporary visibility grant as a reusable directory oracle. Validation, audit,
or settlement failure rolls back the whole listing consumption.

## Memory Views

Processes hold `MemoryView` objects that summarize which objects are visible as
goal, context, evidence, or result state. Fork can attenuate a parent view into
a child. Spawn creates a fresh goal-only view.

A view root is a borrowed reference unless the Object's explicit owner is that
process/subtree. Checkpoint restore uses ownership, not reachability, as its
destructive boundary: restoring a borrower does not roll back the lender's
payload, namespace, owner, or object capability. Checkpoint fork clones owned
reconstructable Objects, while `EXTERNAL_REF` roots and their capabilities are
dropped because a host handle cannot be cloned safely.

`MemoryView.filters` are applied during context materialization after
capability checks and before budget selection. Filters are ORed together; fields
inside one filter are ANDed (`type`, required `tags`, and bounded text search).
Filtered roots are audited as omitted objects.

Merge scaffolding lets a parent merge child-created memory according to a merge
policy. Merge operations still respect process relationships and capabilities.
When a non-root child exits, its process-owned objects remain available for the
direct parent to merge. A merge adopts merged child-owned objects into the
parent and releases unmerged child-owned objects. If the parent exits before
merging, terminal child-owned objects are released during parent cleanup.

When merge authority is finite-use, creation of the parent's derived handle and
consumption of the source grant are one store transaction. Failure to consume
the exact reservation removes the unpublished handle, so a failed merge cannot
leave durable authority behind.

`ObjectPatch()` leaves object payload unchanged. `ObjectPatch(payload=None)`
explicitly writes JSON `null` as the payload.

## Object Tasks

An Object can own background tool tasks. A task records its owner oid, creator
pid, dedicated runner pid, tool, status, result oid, wait information, and
notification state in the runtime store, but it does not persist full tool arguments.
Arguments continue through the existing bounded, redacted tool audit path.

The runner is a child process whose tool table is narrowed to the requested
tool. Starting a task requires the creator to hold `read`, `write`, and `link`
rights on the owner object, `process:spawn` `write`, available per-object and
global ObjectTask concurrency slots, and the requested tool must already be
visible in the creator process tool table. External capabilities are inherited
only when explicitly delegated.

ObjectTask rows are persisted, but active task execution is runtime-local. When
a runtime reopens an existing store, unfinished tasks are marked `abandoned`,
their runner processes are terminalized, and owner pins are cleaned up. The
original tool arguments are not persisted for replay.

Successful tasks keep the tool result as a new Object Memory object and link
the owner object to it with `PRODUCED`. That link is part of the start-time
ObjectTask operation, so one-time owner handles are consumed at start and are
not re-used during completion. The source object payload is not rewritten.
Active tasks pin their owner object so process-exit cleanup cannot release it
before the task reaches a terminal state; once the task is terminal, normal
process-owned memory cleanup can release an owner whose creating process has
already exited.

Task start reserves finite-use owner authority before creating its runner and
commits it with the durable task row. A pre-commit failure removes the runner
before restoring those exact reservations; an executor handoff failure marks
the task failed and removes the unstarted runner. Result creation and linking
use a lifetime scope: failed wiring or a cancellation that wins the terminal
transition releases the unpublished result and derived handles, terminalizes
the runner, and releases the owner pin. Once the succeeded row is durable,
later observability failures do not retract the published result.

The creator can inspect and control its own task without a second owner-object
grant. For another process, `get` and `wait` require owner-object `read`, list
filters to rows visible with `read`, and `cancel` requires `write` after terminal
and unsafe-cancellation preflight. A finite read selected by `get`/`wait` is
claimed once; list claims each distinct finite read used by the returned rows
at most once, and its internal filtering does not spend authority for omitted
rows. Wait polling does not claim the same read repeatedly. Updating an owner
watch also requires owner-object `write` for a non-creator.

Runtime-internal multi-step writes can use an Object Memory lifetime scope.
Objects created in the scope are released automatically unless the scope is
committed or the object is transferred to another owner. This is used for
operations such as tool result creation where a later lifecycle step can still
fail after the Object has been allocated.

An ObjectTask can opt into owner watches. The Object Memory update, append, and
link primitives notify active watching tasks after the object change and audit
record are committed. Notices go to the runner process message queue, use the
`object-task-owner` channel by default, and carry only references such as owner
oid, version, event id, relation, and destination oid. A notice is not a
capability grant; the receiver still needs normal Object Memory authority to
read or materialize any referenced object. Owner-watch messages can resume a
task blocked in `receive_process_messages`; tools with arbitrary side effects
before a message wait are not replayed automatically.
The ObjectTask manager also watches ordinary process messages delivered to a
runner and terminal child-process notices. Those events can resume waiting
tasks only for tools with explicit replay-safe semantics, currently
`receive_process_messages` and `wait_child_process`.

## File/Object Bridge

Bridge tools can move content between workspace files and Object Memory without
returning full file content as a process-visible tool result:

- `create_object_from_file` reads a workspace file through the filesystem
  primitive and creates an object. The resulting Object Memory payload is
  checked against the memory payload hard limit before creation; callers must
  opt into truncation for oversize file objects.
- `write_object_to_file` materializes object payload into a workspace file
  through the filesystem primitive.

This is useful for large content movement and reduces accidental prompt
exposure. It does not bypass filesystem or object capabilities.
`write_object_to_file` accepts only a string payload or a mapping with a string
`content` field; other payload shapes fail instead of being guessed.

## Data labels and provenance

Object metadata carries `sensitivity`, `trust_level`, `integrity`, `origin`,
`tenant`, `principal`, and optional `declassification_authority` labels.
Provenance records parent Object ids and explicit source operation ids. Derived
objects conservatively take the highest source sensitivity and the lowest
source trust/integrity; mixed origins are marked `derived` and mixed tenant or
principal identities are not silently collapsed.

An update that lowers sensitivity or raises trust/integrity requires explicit
`admin` authority on `declassification:object:<oid>`. Context Materialization
Manifests and Explain expose labels alongside ids/hashes without copying Object
payloads. Finite declassification authority is consumed atomically with the
Object update, so a one-shot downgrade grant cannot be reused. External
information-flow intents may record an observe-only label
summary and manifest policy. Version 1 does not yet enforce source-to-sink
policy at Human, JSON-RPC, MCP, network, or file-write sinks.

## Context Materialization

The LLM executor materializes prompt context from process state, event facts,
capability snapshots, object summaries, loaded Skills, and visible tool schemas.
Each process also has a mutable context object named `llm_context:<pid>`.

The runtime appends new process facts and summaries to the end of that object so
repeated prompt prefixes remain stable for prompt caching.

The `compact_process_context` tool is the explicit exception to the append-only
shape: after validation it atomically replaces older entries with one
`context_compacted` summary plus the configured recent verbatim entries.

Materialization budgets use the final rendered object text, not stored
`metadata.token_estimate`. Object creation, payload updates, file imports, and
append-style writes still refresh that estimate as advisory metadata, but stale
or attacker-supplied estimates cannot make enlarged rendered content fit under
the prompt budget.

For LLM execution, the append-only `llm_context:<pid>` render is the charged
context. Source object materialization selects and records deltas without
double-charging the same quantum. The rendered context must fit both the
per-call materialization window and the cumulative materialization budget before
the model is called.

LLM context preparation now persists a metadata-only Context Materialization
Manifest. For each source Object it records oid/version/type, included or
omitted disposition, the exact selection reason, transformation
(`verbatim`, `compacted`, or `truncated`), token count, rendered hash, and data
labels. The
final context Object/version, context generation, effective budget, rendered
tokens, and hash are recorded as well. No extra Object payload or rendered
prompt text is copied into the manifest. Direct materialization returns the
same per-Object selection metadata in memory; durable rows are created when the
final LLM context is prepared. See
[explainable_operations.md](explainable_operations.md).

## Persistence Invariant

Object metadata and namespace directories are stored in the runtime store.
Ordinary object payloads are runtime-only; `objects.payload_json` stores a
runtime-memory marker rather than the user payload for both SQLite and
PostgreSQL backends.

If a reopen cannot materialize a live payload cache for a marker row, the object
is released fail-closed instead of treating the marker as user payload.

Scoped checkpoint snapshots and image artifacts can explicitly capture object
payloads needed to reconstruct a process subtree, subject to configured
snapshot limits.

Root process-owned memory is released on process exit unless retained as the
final process result. Non-root terminal process memory is held only until the
direct parent merges it or exits.
