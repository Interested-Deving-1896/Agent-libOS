# MCP Client Tools

Agent libOS supports client-only MCP Tools for registered external MCP servers.
Agents cannot pass transports, commands, URLs, headers, or raw server
configuration at call time. They pass only:

- `server_id`
- `tool_id`
- `arguments`

v1 covers MCP Tools only. MCP Resources and Prompts are not exposed as runtime
primitives yet.

## Server Manifest V1

Manifests can be YAML or JSON, either as a direct mapping or wrapped under
`mcp_server:` or `server:`.

```yaml
schema_version: 1
server_id: demo-mcp
transport: stdio
stdio:
  command: python3
  args: ["-m", "demo_mcp_server"]
  env:
    DEMO_TOKEN: AGENT_LIBOS_MCP_DEMO_TOKEN
tools:
  - tool_id: forecast
    mcp_name: weather.forecast
    right: read
    rollback_class: no_rollback_required
    state_mutation: false
    information_flow: true
    input_schema:
      type: object
      additionalProperties: true
timeout_s: 10
max_request_bytes: 65536
max_response_bytes: 1048576
```

For Streamable HTTP, use `transport: streamable_http` and an `http:` block:

```yaml
http:
  url: https://api.example.test/mcp
  headers:
    Authorization:
      env: AGENT_LIBOS_MCP_DEMO_TOKEN
      prefix: "Bearer "
```

`stdio.env` maps child process environment variable names to host environment
variable names. The runtime does not inherit the full host environment.

## Authority

Registry metadata authority:

```text
mcp_server:<server_id>
mcp_server:*
```

Tool invocation authority:

```text
mcp:<server_id>:<tool_id>
mcp:<server_id>:*
mcp:*
```

`list_mcp_servers`, `inspect_mcp_server`, and `list_mcp_tools` require server
metadata read authority when called by a process. `call_mcp_tool` requires the
right declared by the tool spec on `mcp:<server_id>:<tool_id>`.

Tool visibility is not authority. Default images can see the MCP tools, but a
process cannot call a registered MCP tool without the matching capability.

## Security Rules

Only manifest-declared tools may be called. Before a call consumes one-shot
tool authority, the primitive asks the provider for live tool metadata and
fails closed if the server no longer exposes the tool or if a pinned
`input_schema` changed.

HTTP transport follows the same default network posture as JSON-RPC: HTTPS for
remote hosts, plain HTTP only for local development hosts, no URL userinfo or
fragments, no literal header secrets, no forbidden request headers, and
environment-backed header secrets restricted by `mcp.header_env_allowlist`.

stdio transport uses argv, not a shell string. The command must be a single
argv token; args are separate strings. Environment injection is explicit and
restricted by `mcp.stdio_env_allowlist`.

MCP call arguments and audit context are bounded and sanitized. MCP result
payloads are JSON-serializable; binary-like content is represented by bounded
metadata rather than raw bytes.

## CLI

```bash
uv run agent-libos --db .agent_libos.sqlite mcp register server.yaml
uv run agent-libos --db .agent_libos.sqlite mcp list
uv run agent-libos --db .agent_libos.sqlite mcp inspect demo-mcp
uv run agent-libos --db .agent_libos.sqlite mcp tools demo-mcp
uv run agent-libos --db .agent_libos.sqlite capabilities grant <pid> mcp:demo-mcp:forecast --rights read
uv run agent-libos --db .agent_libos.sqlite mcp call <pid> demo-mcp forecast --arguments-json '{"city":"Beijing"}'
uv run agent-libos --db .agent_libos.sqlite mcp unregister demo-mcp
```

Registry commands accept `--actor-pid <pid>` to enforce that process's
`mcp_server:*` or exact server capabilities. Without `--actor-pid`, they run as
audited admin registry operations.

The optional SDK-backed provider requires:

```bash
uv sync --extra mcp --all-groups
```

## Tools And Syscalls

LLM-facing tools:

- `list_mcp_servers`
- `inspect_mcp_server`
- `list_mcp_tools`
- `call_mcp_tool`

Deno/TypeScript syscalls:

- `mcp.list`
- `mcp.inspect`
- `mcp.tools`
- `mcp.call`

Syscalls enter the MCP primitive directly. They do not consult the LLM-facing
tool table, and they cannot pass arbitrary transports, URLs, headers, secrets,
or raw MCP tool names.

## Persistence And Checkpoints

MCP server specs are runtime store registry rows. Resolved secret values are
not persisted.

Checkpoint snapshots preserve process capabilities that reference MCP
resources, but they do not copy or restore MCP server registry rows. Restore
and fork can still load capability records, but later inspect/call operations
fail closed if the current runtime does not have a matching registered server.
