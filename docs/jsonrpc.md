# JSON-RPC Over HTTP

Agent libOS supports client-only JSON-RPC 2.0 over HTTP for remote resources.
Remote calls are libOS primitive operations, not ambient network access.

## Boundary

Agents, Skills, and JIT tools never pass URLs, credentials, raw headers, or raw
wire method names at call time. They pass only:

- `endpoint_id`
- `method_id`
- `params`

The runtime resolves endpoint metadata from the JSON-RPC endpoint registry,
checks the caller pid's capabilities, optionally asks the human, performs a
single JSON-RPC HTTP POST through the provider, records audit/events, and writes
a provider-classified external-effect row.

## Endpoint Manifest V1

Endpoint manifests can be YAML or JSON. They may be direct mappings or wrapped
under `jsonrpc_endpoint:` or `endpoint:`.

```yaml
schema_version: 1
endpoint_id: demo-weather
url: https://api.example.test/jsonrpc
headers:
  Authorization:
    env: DEMO_WEATHER_TOKEN
    prefix: "Bearer "
methods:
  - method_id: forecast
    rpc_method: weather.forecast
    right: read
    rollback_class: no_rollback_required
    state_mutation: false
    information_flow: true
    params_schema:
      type: object
      additionalProperties: true
timeout_s: 10
max_request_bytes: 65536
max_response_bytes: 1048576
```

Required endpoint fields:

- `schema_version: 1`
- `endpoint_id`
- `url`
- non-empty `methods`

Required method fields:

- `method_id`
- `rpc_method`
- `right`: `read`, `write`, or `execute`
- `rollback_class`: `irreversible`, `rollbackable`, or
  `no_rollback_required`
- `state_mutation`
- `information_flow`

`method_id` is the capability resource fragment. `rpc_method` is the JSON-RPC
wire method sent in the request body. This separation prevents method-name
punctuation from polluting capability resource matching.

## URL And Credential Rules

Endpoint URLs must be HTTP(S). The default rule is HTTPS only. Plain HTTP is
allowed only for local development hosts: `localhost`, `127.0.0.1`, and `::1`.

The registry rejects:

- URL userinfo,
- URL fragments,
- non-HTTP(S) schemes,
- non-local plain HTTP,
- private, link-local, reserved, multicast, or metadata-service IP targets,
- unsafe endpoint or method ids,
- literal secret header values,
- forbidden request headers such as `Host` or `Content-Length`.

Headers are environment-backed. The registry stores the environment variable
name and optional prefix/suffix, never the resolved secret value. Missing
environment variables fail before the HTTP attempt and before one-shot remote
method authority is consumed.

The default provider does not follow HTTP redirects. Redirects are treated as
HTTP failures so a registered endpoint cannot silently move a call to a new
host.

## Capability Resources

Endpoint metadata authority:

```text
jsonrpc_endpoint:<endpoint_id>
jsonrpc_endpoint:*
```

Method invocation authority:

```text
jsonrpc:<endpoint_id>:<method_id>
jsonrpc:<endpoint_id>:*
jsonrpc:*
```

Method invocation uses the right declared by the method spec. A `read` method
requires `read` on `jsonrpc:<endpoint_id>:<method_id>`. A `write` method
requires `write`, and an `execute` method requires `execute`.

Tool visibility does not grant remote authority. Default images expose
`list_jsonrpc_endpoints`, `inspect_jsonrpc_endpoint`, and
`call_jsonrpc_method`, but a call still fails without the method capability.

## External Effects

The JSON-RPC provider classifies every call from the method spec:

- `rollback_class`
- `rollback_status`
- `state_mutation`
- `information_flow`

The runtime stores an append-only `external_effects` row with provider
`jsonrpc` and operation `call`. Checkpoint restore reports these effects in
`external_effects_since_checkpoint` and `external_effect_summary`. v1 does not
perform remote rollback or compensation.

## CLI

Register and inspect endpoints:

```bash
uv run agent-libos --db .agent_libos.sqlite jsonrpc register endpoint.yaml
uv run agent-libos --db .agent_libos.sqlite jsonrpc list
uv run agent-libos --db .agent_libos.sqlite jsonrpc inspect demo-weather
```

Grant method authority and call as a process:

```bash
uv run agent-libos --db .agent_libos.sqlite capabilities grant <pid> jsonrpc:demo-weather:forecast --rights read
uv run agent-libos --db .agent_libos.sqlite jsonrpc call <pid> demo-weather forecast --params-json '{"city":"Beijing"}'
```

Delete an endpoint:

```bash
uv run agent-libos --db .agent_libos.sqlite jsonrpc unregister demo-weather
```

`--actor-pid <pid>` on registry commands enforces that process's
`jsonrpc_endpoint:*` or exact endpoint capabilities. Method calls always run as
the target pid and are authorized by that pid's method capability.

## Tools And Syscalls

LLM-facing tools:

- `list_jsonrpc_endpoints`
- `inspect_jsonrpc_endpoint`
- `call_jsonrpc_method`

Deno/TypeScript syscalls:

- `jsonrpc.list`
- `jsonrpc.inspect`
- `jsonrpc.call`

Syscalls enter the JSON-RPC primitive directly. They do not consult the
LLM-facing tool table, and they cannot pass arbitrary URLs, headers, secrets,
or raw wire methods.

## Persistence And Checkpoints

Endpoint specs are stored in SQLite as registry rows. Resolved header secret
values are not persisted.

Checkpoint snapshots include JSON-RPC endpoint definitions referenced by the
restored process subtree capabilities. Restore and fork upsert those endpoint
definitions without deleting unrelated registry state.
