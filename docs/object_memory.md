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
- object capabilities.

Object handles carry object-specific rights such as `read`, `write`,
`materialize`, `link`, `diff`, or `delete`.

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

## Memory Views

Processes hold `MemoryView` objects that summarize which objects are visible as
goal, context, evidence, or result state. Fork can attenuate a parent view into
a child. Spawn creates a fresh goal-only view.

Merge scaffolding lets a parent merge child-created memory according to a merge
policy. Merge operations still respect process relationships and capabilities.

## File/Object Bridge

Bridge tools can move content between workspace files and Object Memory without
returning full file content as a process-visible tool result:

- `create_object_from_file` reads a workspace file through the filesystem
  primitive and creates an object.
- `write_object_to_file` materializes object payload into a workspace file
  through the filesystem primitive.

This is useful for large content movement and reduces accidental prompt
exposure. It does not bypass filesystem or object capabilities.

## Context Materialization

The LLM executor materializes prompt context from process state, event facts,
capability snapshots, object summaries, loaded Skills, and visible tool schemas.
Each process also has a mutable context object named `llm_context:<pid>`.

The runtime appends new process facts and summaries to the end of that object so
repeated prompt prefixes remain stable for prompt caching.

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

Process-owned memory is released on process exit unless retained as the final
process result.
