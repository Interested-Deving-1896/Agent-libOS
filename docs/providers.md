# Provider Substrate Reference

The Resource Provider Substrate is the host-effect layer below Agent libOS
primitives. A provider implements a concrete filesystem, clock, subprocess,
Human I/O, JSON-RPC, or MCP backend; it is not an authority bypass. Process
identity, Capability reservation, Human approval, Task Authority Manifest
ceilings, pending-effect persistence, resource settlement, events, and audit
remain primitive/runtime responsibilities.

The protocol types are defined in `agent_libos/substrate/base.py`. The default
composition is `LocalResourceProviderSubstrate` from
`agent_libos/substrate/local.py`; trusted Runtime Modules can register additional
provider hooks during startup. Every real provider boundary must use the
[`ProtectedOperationContract`](protected_operation_sdk.md) lifecycle.

## Current provider inventory

| Provider | Current backend | Authority and policy | Effect/evidence contract | Bounds and containment |
| --- | --- | --- | --- | --- |
| Filesystem | `LocalFilesystemProvider` rooted at one workspace | Typed `filesystem:<namespace>:<path>` rights; cwd and state probes occur only after authority | Reads are information-flow effects; mutations prepare one pending effect and finalize the same id; PENS is accepted only before observation/effect | Lexical resolution followed by no-follow containment, size/list bounds, safe write/delete handling |
| Clock | `LocalClockProvider` | Clock resource/right plus process resource budget | `now`, monotonic observation, sleep/asleep cancellation, and result classification use one protected operation; later failures remain unknown rather than refunding authority | Finite sleep limit, monotonic elapsed accounting, sync and async paths |
| Shell | `LocalShellProvider` | Exact executable resource, shell policy rule, approval, cwd read authority, process budget | Intent exists before spawn; timeout/cancel/limit/classifier failures conservatively retain performed/unknown evidence | No shell command string by default, scrubbed environment, workspace cwd, stdout/stderr bounds, wall/CPU/RSS supervision, process-tree termination |
| Human | `LocalHumanProvider` plus GUI/terminal host surfaces | Typed question/permission request and explicit policy decision | Terminal read/write persists purpose and length/hash observations, not raw prompt/answer/error text | Typed responses, queue state, bounded payload/output, lock-free blocking I/O with claimed request state |
| JSON-RPC | `HttpJsonRpcProvider` | Registered endpoint and exact method capability; model-supplied URLs are forbidden | Registry metadata is gated before lookup; remote DNS starts inside the pending intent; transport/classification settles the same effect id | Header-env allowlist, request/response hard limits, timeout, resolved-address policy, client-only JSON-RPC 2.0 |
| MCP | `SdkMcpProvider` for Streamable HTTP and stdio | Registered server/tool capability; stdio additionally requires `process:spawn` and exact `mcp_stdio:<digest>` execute authority exposed by server inspection | HTTP DNS and stdio spawn are covered by the call/validation intent; registry lookup is gated; tool results use bounded provider evidence | Tools-only v1 surface, manifest/header/stdio-env allowlists, request/response and timeout limits, contained stdio process lifecycle |
| PTY | Trusted `modules/pty` Runtime Module provider hooks | Startup hash trust plus normal process/shell authority; published sessions are Object Memory `EXTERNAL_REF` handles with Object rights | Spawn/write/resize/close each use protected pending-to-finalized effects; cleanup does not erase an uncertain spawn | Independent reader/monitor workers, output bounds, wall/CPU/RSS supervision, POSIX process groups or Windows Job Object/ConPTY support |

The shell provider is containment and mediation, not a general OS sandbox. It
does not make an otherwise hostile native binary safe. Policies restrict
commands and environment, primitives enforce authority, and resource monitors
terminate covered process trees; deployments needing a hostile-code boundary
must inject a container/WASM/service provider and document its own kernel and
network isolation assumptions.

## Failure semantics

An effectful provider may raise `ProviderEffectNotStarted` only when it can
certify that no external mutation, delivery, request, spawn, or information
observation began. Every ordinary exception, timeout, cancellation, resource
limit, missing classifier, or sink failure after dispatch is ambiguous:

- finite authority remains consumed;
- the prepared effect is finalized or retained as `unknown`;
- provider reconciliation may query an existing receipt after restart;
- startup never automatically replays an unknown effect.

Checkpoint restore and image commit report provider-classified effects but do
not compensate or roll back provider state. Audit/effect rows are append-only
evidence only within the RuntimeStore trust boundary: an operator with direct
database write access can tamper with them unless the deployment adds external
append-only storage, signatures, or remote attestation.

## Registration and visibility

JSON-RPC endpoint and MCP server registries are host configuration stored in the
runtime database. Visibility of a registry row, Skill, tool schema, image, or
Runtime Module does not grant process authority. GUI registration accepts
bounded manifest/package payloads for the surfaces it exposes; CLI/admin
registration can use explicit host paths. MCP stdio inspection returns the exact
`stdio_authority_resource` that a host must grant rather than asking users to
reconstruct a digest from command/env fields.

## Provider extension checklist

A new backend or operation is not complete until it has:

1. A typed provider protocol/result and a primitive-facing
   `ProtectedOperationContract`.
2. Explicit capability resource/right, Task Authority effect class, approval
   policy, and resource charge.
3. Prepare-before-observation ordering and exact PENS boundary.
4. Bounded inputs, outputs, time, cancellation, and process/network cleanup.
5. A success classifier plus conservative fallback for classifier failure.
6. Pending-effect reconciliation semantics that query but never replay.
7. Denial, timeout, cancellation, ambiguous outcome, event, audit, and resource
   tests in the appropriate `security` or `providers` lane.
8. Platform coverage or an explicit gap in the
   [support matrix](support_matrix.md).

See [architecture.md](architecture.md) for composition,
[capabilities.md](capabilities.md) for authority resources,
[jsonrpc.md](jsonrpc.md) and [mcp.md](mcp.md) for remote manifests, and
[modules.md](modules.md) for trusted provider-hook registration.
