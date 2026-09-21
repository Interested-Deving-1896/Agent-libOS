# Runnable Host extension examples

From a Git checkout containing `uv.lock`, install the locked development
environment with `uv sync --frozen`, then run the commands below from the
repository root. Python 3.11+ is required. Neither example calls an
LLM or a network service, reads credentials, requires Deno, or loads the
project's CLI configuration. Each uses explicit `DEFAULT_CONFIG`, an in-memory
Runtime database, and a temporary workspace removed on exit.

An extracted source distribution contains these examples but omits `uv.lock`.
Follow the [source-distribution installation instructions](../../docs/development.md#source-distribution-installation-smoke),
then replace `uv run python` below with `.venv-sdist/bin/python` (or
`.venv-sdist/Scripts/python.exe` on Windows). That installation resolves the
published dependency bounds afresh; it is not a frozen-lock reproduction.

## Python tool

```sh
uv run python examples/extensions/python_tool.py
```

Expected output:

```json
{"denied_before_grant": true, "result": {"bytes_read": 6, "truncated": false}, "tool_visible": true}
```

The script defines strict Pydantic input/output schemas and a `SyncAgentTool`,
registers it with `ToolBroker`, and binds it to one process. A call first fails
because tool visibility grants no filesystem authority. Trusted Host code then
issues one read of `filesystem:workspace:note.txt`, and the same call succeeds
through the filesystem primitive. The sample UTF-8 text is two characters and
six bytes. Setup writes the fixture as Host code; the model-facing tool reads
only through `ctx.runtime.filesystem`.

`configure_process_tools` replaces both tool tables. This example deliberately
binds just one tool; a larger application must retain its other intended
bindings. The Host invokes `runtime.tools.call` directly, so no scheduler or
model selection is needed. Blocking Python tools rely on primitive/provider
deadlines; `ToolPolicy.timeout_s` cannot kill their worker threads.

## Protected provider operation

```sh
uv run python examples/extensions/protected_operation.py
```

Expected output:

```json
{"audit_and_event_linked": true, "denied_after_one_use": true, "effect_state": "committed", "provider_calls": 1, "result": "hello"}
```

`NoticeProvider` is an in-memory fixture for a bounded external read. Its
`classify_external_effect(operation, context, result)` returns the public
`ExternalEffectClassification` type. The primitive-like `read_notice` facade
checks a process Capability, supplies trusted unclassified ingress context,
preflights read usage, dispatches exactly one SDK phase, then commits safe
event/audit/effect evidence and measured usage. The second call is denied after
the one-use grant is consumed; assertions verify that the provider is not
called again and no second effect appears. The observation contains identifiers
and byte counts, not the provider's returned content.

The example uses ordinary Capability authority and the default Task Authority
policy. A Host that sets an explicit effect ceiling must also allow
`example.read_notice`. It has no outgoing payload, so its contract is `ingress`
and needs no Sink. Real bidirectional/egress integrations must supply the Sink,
source context, canonical payload, and operation described in the SDK guide.
The fixture has no blocking I/O; a real provider must additionally enforce its
own time, size, cancellation, and transport limits. Registration and
`issue_trusted` belong to trusted Host composition, never to model-selected code.

See [Python tools](../../docs/tools_and_jit.md#writing-python-tools),
[Protected Operation SDK](../../docs/protected_operation_sdk.md), and
[provider extension requirements](../../docs/providers.md#provider-extension-checklist).
