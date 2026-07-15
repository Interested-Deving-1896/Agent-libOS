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
failures, resource-budget preflight failures, and preflight external-effect
classifier failures happen before finite method authority is reserved. DNS is
different: the reservation and pending effect intent are durable first because
resolving a non-local host is itself an external information-flow boundary. A
successful lookup or an ordinary failure after host observation commits the
use even though no HTTP request was sent.

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

These per-item checks occur before the store loads existing endpoint metadata,
so unauthorized register/replace/inspect/unregister attempts cannot distinguish
an existing id from a missing one. `replace=true` always requests `admin` and a
non-replace registration always requests `write`; the right does not depend on
an existence lookup. Registration, replacement, and unregistration commit the
endpoint row, stale method-grant invalidation, event, and audit in one store
transaction. Finite registry authority is reserved after duplicate/not-found
preflight and committed inside that same transaction, so validation or
event/audit failure leaves the exact one-shot grant available.

Tool visibility does not grant remote authority. Default images expose
`list_jsonrpc_endpoints`, `inspect_jsonrpc_endpoint`, and
`call_jsonrpc_method`, but a call still fails without the method capability.

## Data-flow Sink

`params` is an egress payload to
`jsonrpc:<endpoint_id>:<method_id>`. After the authority-before-lookup visibility
gate, the runtime hashes the complete endpoint plus selected method manifest as
the Sink configuration identity. A Host trust rule above `normal` must bind
that hash, along with its sensitivity and tenant/principal clearance. Replacing
the URL, wire method, schema, headers, limits, or effect metadata changes the
identity and invalidates old trust.

Clearance is checked before ordinary per-use approval, environment resolution,
DNS, or transport and is revalidated with source Object versions and canonical
params in the protected-operation transaction. A trusted endpoint still needs
the exact JSON-RPC method capability, Task Authority effect permission, and
budget. A conditional high-sensitivity call needs an exact metadata-only
release; an untrusted endpoint cannot be elevated above `normal`. See
[Data Flow](data_flow.md).

## External Effects

The JSON-RPC provider classifies every call from the method spec:

- `rollback_class`
- `rollback_status`
- `state_mutation`
- `information_flow`

After schema/environment/request-size/budget/classifier preflight, the runtime
atomically reserves finite method authority and creates an `external_effects`
row with provider `jsonrpc`, operation `call`, and `effect_state: pending`.
Only then does it perform runtime DNS resolution and the live provider call.
Successful or failed transport results emit event/audit evidence, run the post-call
classifier, and CAS that same `effect_id` to `finalized`. A post-call classifier
failure falls back conservatively instead of dropping the effect. If event,
audit, or finalization fails after the transport may have run, the row remains a
durable pending/unknown effect for checkpoint and benchmark reporting.

`ProviderEffectNotStarted` conditionally abandons only when the first DNS
boundary itself certifies that it did not start. Once DNS returned or otherwise
observed the host, a later not-started transport finalizes
`state_mutation=false, information_flow=true` and the reservation stays
committed. Ordinary DNS/transport errors, non-2xx responses, and JSON-RPC error
results are finalized outcomes. Checkpoint restore reports finalized and
pending rows in `external_effects_since_checkpoint` and
`external_effect_summary`. v1 does not perform remote rollback or compensation.

Audit and external-effect metadata store bounded, redacted observations of
`params` with size and hash. Raw params are sent to the registered provider but
are not persisted in audit or provider-effect context.

Successful effect metadata additionally carries the data-flow decision,
registry generation, trust id/hash, label/source hashes, and exact source
Object references, never raw source payloads.

`params_schema`, when present, is validated at registration time and enforced
before each call. Parameter validation failures do not contact the provider and
do not consume one-shot method authority.
After local checks pass, a one-shot method grant is reserved in the same
transaction as pending-effect persistence. It is restored only for a certified
failure before DNS or any other information flow. DNS observation, transport
errors, non-2xx responses, JSON-RPC error results, or a later
certified-not-started transport do not mint another remote-call use.

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
row replacement, stale method-grant invalidation, event, and audit happen in
one store transaction; if any part fails, the old endpoint spec remains active.
Unregistering an endpoint also invalidates exact and wildcard method grants for
that endpoint in the same transaction, so reusing the same endpoint id cannot
revive stale method authority.

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
