# Explainable Operations

Explainable Operations is the host-side provenance query layer for protected
runtime work. It answers what ran, why authority allowed or denied it, which
human decision or finite-use reservation participated, whether a provider
effect was finalized or remained uncertain, and which resource/context records
were produced.

The explanation is deterministic. It uses persisted identifiers and typed
links; it does not call an LLM and does not associate records merely because
their timestamps are close.

## Operation Tree

Every record has an `operation_id`, a causal root, and an optional parent:

```text
llm_request
  -> tool_call
     -> syscall
        -> primitive
     -> primitive
  -> runtime operation
```

One LLM action-selection cycle is one logical `llm_request`; provider repair
attempts are separate `llm_call` evidence under that operation. Parallel tools
are sibling operations. A protected host call without an active parent becomes
a root operation.

Human, child-process, and message waits set the affected LLM/tool operations to
`waiting`. Durable pending actions store both ids, and resume re-enters those
same operations even though a new concrete tool-call attempt may receive a new
`call_id`. ObjectTask execution links its background tool operation to the
persisted task-start operation; Human resume reuses the operation linked to the
request.

At runtime reopen, an operation left `running` is marked `interrupted`. A
durable `waiting` operation remains waiting. An uncertain pending provider
effect makes the affected primitive/tool and enclosing LLM outcome `unknown`;
this is distinct from missing evidence. The same rule applies after settlement
has finalized an effect with `transaction_state=unknown`; bookkeeping
finalization does not turn an uncertain provider outcome into an ordinary
failure.

## Evidence

`operation_evidence` explicitly relates operations to these roles:

- invocation and result;
- capability decision and finite-use reservation;
- Human approval or wait;
- provider effect;
- resource charge;
- context manifest;
- event and audit.

Data-flow decisions are append-only evidence as well. Their linked audit/event
records carry the decision id, Sink/trust hashes, registry generation, source
references, label summary, and release id without copying the payload. A
pre-provider data-flow denial therefore remains explainable even though no
external-effect intent exists and no ordinary finite-use capability was
consumed.

Audit and event managers attach every record emitted inside an active operation.
Capability, Human, external-effect, ToolBroker, LLM, ObjectTask, and context
code additionally link their own durable identifiers. A uniqueness constraint
on `(operation_id, evidence_type, evidence_id, role)` makes repeated attachment
idempotent. Evidence pagination groups all roles for one evidence identity
before applying the page limit, so an audit carrying both `audit` and
`decision` roles is never split into duplicate timeline rows.

Each operation carries `expected_roles`. Authorization adds `decision` only
when a real capability decision is made. Crossing a provider boundary adds
`effect`, `event`, and `audit`. Therefore an operation denied before the
provider is not incorrectly reported as missing effect evidence.

The [Protected Operation SDK](protected_operation_sdk.md) declares these roles
from the registered contract and links the prepared/finalized effect to the
same primitive operation. Multi-phase DNS, validation, transport, and cleanup
steps do not rely on timestamp correlation or create competing root causes.

`evidence_complete` means all declared roles have at least one explicit link.
It is provenance completeness, not a security or semantic-quality score.
`missing_evidence` names the operation and role; `uncertainties` separately
reports waiting, interruption, unknown outcome, or a pending provider effect.

## Context Materialization Manifest

Each LLM action-selection records one metadata-only manifest containing:

- process/view id, policy, effective token budget, and context generation;
- final context Object id/version, rendered tokens, and SHA-256;
- each source Object id/version/type;
- included/omitted disposition and reason (`selected`, `filter_mismatch`,
  `capability_denied`, `token_budget`, or `missing`);
- transformation (`verbatim`, `compacted`, or `truncated`) and rendered token/hash
  metadata.

The manifest does not copy Object payloads, rendered prompt text, Human answers,
or provider responses. Direct `memory.materialize_context` calls return the same
per-Object selection metadata in memory; durable manifest rows are created when
the final LLM context is prepared.

## Host Interfaces

CLI:

```bash
agent-libos --db <store> explain process <pid>
agent-libos --db <store> explain operation <operation_id>
agent-libos --db <store> explain call <call_id>
agent-libos --db <store> explain effect <effect_id>
agent-libos --db <store> explain request <request_id>
agent-libos --db <store> explain audit <record_id>
agent-libos --db <store> explain event <event_id>
agent-libos --db <store> explain reservation <reservation_id>
agent-libos --db <store> explain context <materialization_id>
```

HTTP:

- `GET /api/operations?pid=...&limit=...&cursor=...`
- `GET /api/operations/{operation_id}?evidence_limit=...&cursor=...`
- `GET /api/operations/resolve?kind=...&id=...`

The process list returns causal roots; operation detail expands the selected
root into all explicitly linked descendants.

No match returns `404`. An evidence id that maps to multiple causal roots
returns explicit candidates (`409` over HTTP); the runtime never chooses by
time or similarity.

The Electron GUI provides an Explain tab with outcome/completeness summary,
causal tree, filtered evidence timeline, and pagination. Audit, event, LLM, and
Human timeline entries can open their linked operation. Snapshot/SSE changes
remount the panel against current evidence.

## Visibility and Redaction

Explain is available only to host CLI and the authenticated local GUI API. It
is not a model tool or process syscall.

Responses preserve routing ids, statuses, rights, targets, hashes, counts, and
rollback classification. They apply observability redaction to decisions,
payload-like fields, credentials, Human content, LLM raw I/O, Object payloads,
raw command arguments and environments, stdout/stderr, and provider metadata.
The original append-only audit/effect records remain unchanged.

Old unlinked rows are not backfilled or heuristically reconstructed. Opening an
older store creates the additive tables, but only newly recorded explicit links
can be reported as complete explanations.
