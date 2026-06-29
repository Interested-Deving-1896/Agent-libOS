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
single primitive/provider call through the JSON-RPC provider, records
audit/events, and writes a provider-classified external-effect row. The default
HTTP provider may try another previously validated pinned address if connection
setup fails before a response is received; endpoint methods must not rely on a
single wire-level POST attempt for non-idempotency guarantees.
For call attempts, the primitive first constructs the capability resource from
`endpoint_id` and `method_id` and requires call authority before loading the
endpoint manifest or method schema. A caller without invocation authority gets a
generic denial and cannot use call errors to enumerate registered endpoint
metadata. This early visibility gate does not consume a one-shot method grant;
the exact method is then authorized after the method spec is known, and any
one-shot use from that decision is consumed only after pre-provider validation
has passed.

## Endpoint Manifest V1

Endpoint manifests can be YAML or JSON. They may be direct mappings or wrapped
under `jsonrpc_endpoint:` or `endpoint:`.

```yaml
schema_version: 1
endpoint_id: demo-weather
url: https://api.example.test/jsonrpc
headers:
  Authorization:
    env: AGENT_LIBOS_JSONRPC_DEMO_WEATHER_TOKEN
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

- `endpoint_id`
- `url`
- non-empty `methods`

`schema_version` is optional and defaults to `1` when omitted. Repository
manifests should include it explicitly so future migrations are visible in
review.

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
- DNS results that resolve a non-local endpoint to loopback, private,
  link-local, reserved, multicast, or other non-public addresses,
- unsafe endpoint or method ids,
- literal secret header values,
- header prefixes outside the approved auth-scheme prefixes and any non-empty
  header suffix,
- forbidden request headers such as `Host` or `Content-Length`.

Headers are environment-backed. The registry stores the environment variable
name and a small approved prefix such as `Bearer `, never the resolved secret
value. Missing environment variables, parameter schema failures, request-size
failures, resource-budget preflight failures, external-effect classifier
failures, and runtime DNS policy failures happen before the HTTP attempt and
before one-shot remote method authority is consumed.
For remote HTTPS calls, the primitive passes the validated address set to the
default provider, which opens the socket to one of those exact addresses while
preserving the original Host header and TLS server name. This prevents a host
from passing runtime DNS policy and then being re-resolved by the HTTP client to
a different private or loopback address.

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

Endpoint registry operations use endpoint metadata authority:

| Operation | Required capability when `--actor-pid` is used |
| --- | --- |
| list endpoints | `jsonrpc_endpoint:* read` |
| inspect endpoint | `jsonrpc_endpoint:<endpoint_id> read` |
| register new endpoint | `jsonrpc_endpoint:<endpoint_id> write` |
| replace endpoint | `jsonrpc_endpoint:<endpoint_id> admin` |
| unregister endpoint | `jsonrpc_endpoint:<endpoint_id> admin` |

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

Audit and external-effect metadata store bounded, redacted observations of
`params` with size and hash. Raw params are sent to the registered provider but
are not persisted in audit or provider-effect context.

`params_schema`, when present, is validated at registration time and enforced
before each call. Parameter validation failures do not contact the provider and
do not consume one-shot method authority.
After those pre-provider checks pass, a one-shot method grant is consumed just
before the provider call. Transport errors, non-2xx responses, or JSON-RPC
error results after that boundary do not restore the use.

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

Replacing an existing endpoint requires endpoint `admin` when an actor pid is
used. A replace invalidates existing exact method grants for that endpoint so
old authority cannot silently point at a new URL or wire method. The endpoint
row replacement and stale method-grant invalidation happen in one store
transaction; if either part fails, the old endpoint spec remains active.
Unregistering an endpoint also invalidates exact and wildcard method grants for
that endpoint, so reusing the same endpoint id cannot revive stale method
authority.

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

Endpoint specs are stored as runtime store registry rows. Resolved header secret
values are not persisted.

Checkpoint snapshots preserve process capabilities that reference JSON-RPC
resources, but they do not copy or restore endpoint registry rows. Restore and
fork can still load the capability records, but later inspect/call operations
fail closed if the current runtime does not have a matching registered
endpoint. A host operator must register provider configuration explicitly.
