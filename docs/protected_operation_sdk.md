# Protected Operation SDK

`agent_libos.sdk` is the stable extension boundary for provider-backed host
operations. It combines capability reservation, Task Authority Manifest effect
ceilings, canonical argument binding, provider dispatch, effect
classification, event/audit evidence, resource settlement, and Explainable
Operations linkage in one fail-closed state machine. A tool or extension must
not call the runtime-internal effect lifecycle helpers directly.

The SDK does not replace domain authorization or provider interfaces. The
primitive still validates arguments, chooses the exact capability decisions,
and supplies safe evidence. The SDK controls when those decisions are reserved
and committed and how provider ambiguity is represented.

## Contract and invocation

Register contracts during trusted Runtime composition:

```python
from agent_libos.sdk import ProtectedOperationContract, ResourcePolicy

runtime.protected_operations.register_contract(
    ProtectedOperationContract(
        name="primitive.example.fetch",
        provider="example",
        operation="fetch",
        evidence_roles=("audit", "event", "effect"),
        resource_policy=ResourcePolicy.REQUIRED,
        information_flow=True,
    )
)
```

`AuthorityMode.CAPABILITY` is the default and requires one or more allowed
`CapabilityDecision` values for the acting pid. `AuthorityMode.RUNTIME_INTERNAL`
requires a non-empty `internal_reason`; it is for a Runtime-owned continuation,
not a shortcut for extension code. `state_mutation` and `information_flow` are
conservative upper bounds used when the provider outcome or classifier is
unknown. `ResourcePolicy` declares whether accounting is forbidden, optional,
or requires preflight plus resource settlement after every durably settled
dispatched outcome. A required operation that fails after dispatch uses
`failure_resource` when the invocation can measure partial usage; otherwise the
SDK conservatively charges the preflight usage after the failure effect is
durably settled. Provider failure therefore cannot create an unmetered
finalized path; a failed effect settlement remains a pending intent for
reconciliation.

If `prepare` changes durable domain state, declare a named
`prepared_recovery` policy on the contract and register its trusted recovery
handler during Runtime composition. The effect row persists only the policy
name, safe observation, and reservation IDs. On startup the SDK runs that local
handler, restores still-live reservations, and abandons the intent before the
general provider reconciler runs. A `prepared` SDK intent is never sent to a
provider reconciler because no provider phase was dispatched.

An invocation contains full canonical arguments and a separate safe
observation. The SDK hashes canonical arguments for approval/idempotency
binding but persists only the observation:

```python
invocation = ProtectedOperationInvocation(
    pid=pid,
    actor=pid,
    target="example:item-7",
    decisions=(decision,),
    canonical_args={"item": "item-7", "credential": credential},
    observation={"item": "item-7", "credential_present": True},
    preflight_usage=ResourceUsage(external_read_bytes=max_bytes),
    resource_source="primitive.example.fetch",
    failure_resource=lambda error, phase: ResourceSettlement(
        usage=measured_partial_usage(),
        source="primitive.example.fetch",
        context={"failure_phase": phase},
    ),
)
```

Omit `failure_resource` only when conservative preflight charging is the right
failure policy. Its factory runs after the provider effect has been classified
and settled, receives only the exception object and safe phase name, and must
not make a provider call or place exception text in persisted context.

Do not put Object payloads, credentials, Human content, raw LLM I/O, provider
payloads, stdout/stderr, or exception text in observations or evidence.

## Synchronous operation

Every real provider call is a named phase:

```python
with runtime.protected_operations.start(
    "primitive.example.fetch", invocation, provider=provider
) as operation:
    response = operation.call(
        ProviderPhase("transport", information_flow=True),
        provider.fetch,
        item_id,
    )
    return operation.complete(
        response,
        ProtectedOperationEvidence(
            event_type=EventType.EXTERNAL_READ,
            event_source=pid,
            event_target="example:item-7",
            event_payload={"status": response.status},
            audit_action="primitive.example.fetch",
            audit_actor=pid,
            audit_target="example:item-7",
            audit_decision={"status": response.status},
            effect_metadata={"status": response.status},
            provider_receipt={"receipt_id": response.receipt_id},
        ),
        classification_result={"status": response.status},
        resource=ResourceSettlement(
            ResourceUsage(external_read_bytes=response.bytes_read),
            source="primitive.example.fetch",
        ),
    )
```

`complete()` atomically runs the local `settle_success` hook, emits event/audit,
and finalizes the prepared effect id. Resource charge runs afterward. A charge
or overage error is reported to the caller and may terminate the process, but
cannot hide or roll back an already committed provider effect.

## Async and composite providers

Use `acall()` for an async provider. Composite operations use sibling phases on
the same handle:

```python
addresses = operation.call(
    ProviderPhase("dns", information_flow=True),
    resolve_registered_host,
    endpoint,
)
reply = await operation.acall(
    ProviderPhase("transport", state_mutation=True, information_flow=True),
    provider.acall,
    endpoint,
    addresses,
)
```

After the first phase that may have observed or changed provider state, finite
authority is committed. If a later phase raises `ProviderEffectNotStarted`, the
SDK finalizes the confirmed partial effect; it does not erase the earlier
information flow or restore authority.

When a primitive must return a structured domain error instead of propagating
`ProviderEffectNotStarted`, its provider callable may return
`ProviderEffectNotStartedResult(error, result, outcome=...)`. The SDK settles
the certificate without adding the current phase to the completed-phase floor;
the primitive returns `result` only after observing that marker. This prevents
a not-started mutating phase from becoming a false committed mutation.

## Failure and at-most-once behavior

- Failure before dispatch atomically restores revoke-safe reservations, runs
  `restore_not_started`, and abandons the prepared intent.
- `ProviderEffectNotStarted` in the first provider phase has the same result.
- An ordinary exception or cancellation consumes authority and best-effort
  finalizes `unknown`; if settlement itself fails, the dispatched pending intent
  remains durable for reconciliation.
- A dispatched required-resource operation settles measured `failure_resource`
  usage, or conservatively settles its preflight usage when no measurement is
  available. Charging remains after effect settlement, so an overage cannot
  erase the provider outcome.
- A classifier failure uses the contract's conservative effect ceiling and
  records only the classifier error type.
- Exiting without a provider phase or without `complete()`, completing twice,
  or charging resources contrary to the contract raises
  `ProtectedOperationProtocolError`.

Human terminal output uses
`PostProviderFailureMode.PRESERVE_RESULT`. Once the sink accepts content, a
later local settlement failure returns the accepted result, keeps pending
unknown evidence, and never invokes the sink again.

## Prepare, settle, and compensation hooks

`prepare`, `restore_not_started`, and `settle_success` may mutate only local
transactional state. They must not call a provider. A compensating host action
is another phase on the same handle:

```python
try:
    handle = operation.call(ProviderPhase("create", state_mutation=True), provider.create)
    publish_local_handle(handle)
except Exception:
    if handle is not None and not operation.terminal:
        operation.call(ProviderPhase("cleanup", state_mutation=True), handle.close)
    raise
```

Never blindly retry an `unknown` provider outcome. Startup reconciliation may
query an idempotency key or receipt when the provider explicitly supports it;
otherwise the effect stays `unknown`.

## Enforcement

`scripts/check_protected_operations.py` rejects provider subsystem imports or
calls of the runtime-internal prepare/dispatch/finalize/abandon helpers and use
of the former private reservation-restoration API. It also rejects direct
`self.provider.*()` calls that are not reachable from an SDK `call()`/`acall()`
phase (apart from explicitly enumerated non-effect path normalization). The
check is call-site-sensitive: a helper that reaches a provider may not also be
called outside a phase, and provider session-handle calls such as
`session.handle.read()` must be inside a protected callable. The SDK contract
registry is checked against the Explainable Operations external primitive
boundary set.
The SDK is Host-only infrastructure: it adds no model tool, syscall, CLI, or
HTTP endpoint.
