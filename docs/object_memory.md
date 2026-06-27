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

## Memory Views

Processes hold `MemoryView` objects that summarize which objects are visible as
goal, context, evidence, or result state. Fork can attenuate a parent view into
a child. Spawn creates a fresh goal-only view.

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

`ObjectPatch()` leaves object payload unchanged. `ObjectPatch(payload=None)`
explicitly writes JSON `null` as the payload.

## Object Tasks

An Object can own background tool tasks. A task records its owner oid, creator
pid, dedicated runner pid, tool, status, result oid, wait information, and
notification state in SQLite, but it does not persist full tool arguments.
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

## Context Materialization

The LLM executor materializes prompt context from process state, event facts,
capability snapshots, object summaries, loaded Skills, and visible tool schemas.
Each process also has a mutable context object named `llm_context:<pid>`.

The runtime appends new process facts and summaries to the end of that object so
repeated prompt prefixes remain stable for prompt caching.

The `compact_process_context` tool is the explicit exception to the append-only
shape: after validation it atomically replaces older entries with one
`context_compacted` summary plus the configured recent verbatim entries.

Materialization budgets use each object's current `metadata.token_estimate`.
Object creation, payload updates, file imports, and append-style writes refresh
that estimate so enlarged payloads cannot slip into prompts under stale budget
metadata.

For LLM execution, the append-only `llm_context:<pid>` render is the charged
context. Source object materialization selects and records deltas without
double-charging the same quantum. The rendered context must fit both the
per-call materialization window and the cumulative materialization budget before
the model is called.

Current context materialization does not yet expose complete per-call metadata
for every included, omitted, summarized, or truncated object. That remains a
known gap for future audit explain work.

## Persistence Invariant

Object metadata and namespace directories are stored in SQLite. Ordinary object
payloads are not stored as durable `objects.payload_json` rows; they live in
runtime memory.

Scoped checkpoint snapshots are the explicit exception. A checkpoint can
capture object payloads needed to reconstruct a process subtree, subject to
configured snapshot limits.

Root process-owned memory is released on process exit unless retained as the
final process result. Non-root terminal process memory is held only until the
direct parent merges it or exits.
