# Startup Runtime Modules

Runtime Modules are trusted Python extensions loaded before `Runtime.open()`
returns. They are for extending the runtime composition root, not for giving an
AgentProcess extra authority.

## Boundary

Modules are part of the host trusted computing base:

- They run in the Python interpreter, not in the Deno/JIT sandbox.
- They are loaded only from explicit manifests.
- The entrypoint source file must match the manifest `sha256`.
- The `(module_id, source_sha256)` pair must be trusted by config or CLI.
- Loading a module never grants filesystem, shell, Object Memory, human,
  process, checkpoint, Skill, image, or JSON-RPC capabilities to a process.

Use Skills when the model needs workflow instructions, tool visibility, or JIT
tool candidates at runtime. Use Runtime Modules when the system owner wants to
install new host-side runtime surfaces before startup.

## Manifest V1

```yaml
schema_version: 1
module_id: example-module:v0
name: Example module
version: v0
entrypoint: ./example_module.py:register_module
provides:
  tools:
    - example_echo
  images:
    - example-agent:v0
  syscalls:
    - example.ping
  provider_hooks: []
  startup_hooks:
    - initialize_example
sha256: "<sha256 of example_module.py bytes>"
metadata:
  owner: local
```

The manifest may also use an import-string entrypoint such as
`example_package.module:register_module`. File entrypoints are resolved relative
to the manifest directory and cannot escape that directory with `../`.

## Entrypoint

The entrypoint is synchronous:

```python
def register_module(ctx):
    ctx.register_tool(MyTool())
    ctx.register_image(my_image)
    ctx.register_syscall("example.ping", ping_handler)
    ctx.add_startup_hook(initialize_example)
```

The module context buffers registrations first. The registry verifies that the
module registered only declared resources, checks name collisions, then applies
the registrations before the runtime is returned to the caller.

## Registration Surfaces

`ctx.register_tool(tool)` registers a static model-facing tool through
`ToolBroker`. Tool visibility remains separate from resource authority.

`ctx.register_image(image)` registers an `AgentImage` through the image
primitive validation path. Image `required_capabilities` are applied only by
normal process bootstrap rules and never by module loading itself.

`ctx.register_syscall(name, handler)` adds a module syscall to the syscall
router. The handler receives the `LibOSSyscallSession` and syscall args. It
must call existing primitives for protected effects.

`ctx.register_provider_hook(kind, hook)` records a trusted provider hook for
runtime-level extension code. Hooks are startup module code, not process
capabilities.

`ctx.add_startup_hook(fn)` runs a synchronous hook after all startup module
registrations have been applied.

## CLI

Verify a manifest without loading it:

```bash
uv run agent-libos modules verify modules/example/module.yaml
```

Load a trusted module before a command:

```bash
uv run agent-libos \
  --module-manifest modules/example/module.yaml \
  --trusted-module example-module:v0:<source_sha256> \
  modules list
```

The weaker `--trusted-module-sha256 <sha256>` trusts any module id with that
source hash and is intended for local development only.

Every command that needs module-provided images, tools, or syscalls must pass
the same startup module configuration, or use an application-level config that
sets `AgentLibOSConfig.modules.manifest_paths` and trusted hashes.

## Persistence And Checkpoints

The `runtime_modules` table records loaded and failed modules, source hashes,
entrypoints, and registration summaries. This is audit and reproducibility
metadata, not a dynamic loading authority for processes.

Checkpoint snapshots record the currently loaded module summaries. Restore and
fork fail if the current Python runtime has not loaded the same required module
ids and source hashes. Checkpoint restore does not load Python modules and does
not roll back the module environment.
