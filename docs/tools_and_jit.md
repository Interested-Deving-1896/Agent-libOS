# Tools And Deno/TypeScript JIT

LLM-facing tools are stable wrappers over libOS primitives. They provide names,
schemas, validation, and model ergonomics. Primitives enforce authority.

## Built-In Tools

The current built-in tool surface includes tools for:

- Object Memory: create, append, read, list namespaces, and bridge objects to
  files.
- Filesystem: read/write text, list/create/delete directories, and delete files.
- Human I/O: ask questions, output messages, and request permission.
- Clock: current time and async sleep.
- Process lifecycle: fork, spawn, wait, list children, signal, merge memory,
  exec, exit, cwd get/set, and process messages.
- Shell: argv-only subprocess execution through policy.
- JSON-RPC: list/inspect registered endpoints and call registered methods.
- Image registry: load image manifests from YAML.
- Checkpoint: create, list, inspect, diff, restore, and fork.
- Skills: discover, inspect, load, unload, and load from workspace YAML.
- JIT: propose, validate, and register Deno/TypeScript tools.
- Utility actions such as `echo` and `parse_pytest_log`.

Use `uv run agent-libos tools` to inspect registered tools in a runtime.

## Writing Python Tools

Python tools should not directly access host resources. Use this pattern:

1. Define a Pydantic input schema and optional output schema.
2. Subclass `SyncAgentTool` for blocking local code or `BaseAgentTool` for
   async code.
3. Keep validation and model-facing ergonomics in the tool.
4. Call `ctx.runtime.<primitive>` for process, memory, filesystem, human,
   clock, shell, image, Skill, checkpoint, or other libOS operations.
5. Let primitives enforce capability checks, containment, audit, events, human
   approval, checkpoint semantics, and policy hooks.
6. Register the tool through the runtime composition root or ToolBroker-backed
   registry.

Do not put direct filesystem, terminal, network, shell, browser, database, or
credential access inside a model-facing tool unless that code is itself the
libOS primitive or a sandbox backend.

## JIT Tool Lifecycle

Agent-authored JIT tools use TypeScript and run under Deno. Python JIT tools
are intentionally not supported.

The manual lifecycle is:

1. `propose_jit_tool`: store candidate metadata and TypeScript source.
2. `validate_jit_tool`: run static checks, import allowlist checks, and
   configured tests.
3. `register_jit_tool`: add the validated tool only to the registering process
   tool table.

Skill loading uses the same validation and registration path for bundled JIT
tools.

## TypeScript Entry Point

The TypeScript module must export `run(args, libos)`:

```ts
export async function run(args, libos) {
  const file = await libos.syscall("filesystem.read_text", { path: args.path });
  return { bytes: String(file.content ?? "").length };
}
```

`run` may be synchronous or async. The only libOS access channel is:

```ts
await libos.syscall(name, args)
```

The `libos` object does not expose Python objects, `Runtime`, or
`runtime.tools`.

## RPC Protocol

Python starts a Deno subprocess and writes one NDJSON run frame:

```json
{"type":"run","args":{}}
```

TypeScript may emit syscall frames:

```json
{"type":"syscall","id":"1","name":"filesystem.read_text","args":{"path":"README.md"}}
```

Python responds with final syscall results:

```json
{"type":"syscall_result","id":"1","ok":true,"payload":{}}
{"type":"syscall_result","id":"1","ok":false,"error":"permission denied"}
```

The tool returns:

```json
{"type":"result","value":{}}
```

There is no public pending/retry state for human approval, child wait, or
message wait. Blocking is an implementation detail inside the syscall.

## Syscall Semantics

JIT syscalls enter `LibOSSyscallSession`. They are authorized by:

- caller pid,
- primitive-level capability checks,
- permission policy,
- human approval,
- provider containment,
- audit and event emission.

They do not consult the caller's LLM-facing tool table. This is deliberate:
tool visibility and resource authority are separate.

The current syscall surface covers existing primitive areas:

- filesystem read/write/list/mkdir/delete,
- memory namespace/object read/write/list/append,
- human ask/output/request permission,
- clock now/sleep,
- process cwd/fork/spawn/wait/list/signal/merge/exec/exit/messages,
- shell run,
- JSON-RPC list/inspect/call,
- image load/register,
- checkpoint create/list/inspect/diff/restore/fork/replay,
- Skill discover/inspect/register/load/unload/load YAML.

## Sandbox Rules

Deno is launched with `--no-prompt` and without read, write, net, env, run, or
ffi host permissions. External effects must go through syscalls.

Static imports are limited to configured `jsr:` packages. The default allowlist
is a small `@std/*` subset. `npm:`, `node:`, `http:`, `https:`, `file:`,
dynamic imports, and unsafe host APIs are rejected.

If Deno is missing, validation returns a clear error. Python unit tests skip or
mock true Deno execution where appropriate.

## Deferred Lifecycle

`process.exit` and `process.exec` are normal syscalls from TypeScript. Calling
them does not terminate the Deno subprocess mid-protocol. The runtime records
the lifecycle change and applies it after the JIT tool returns its normal
result.
