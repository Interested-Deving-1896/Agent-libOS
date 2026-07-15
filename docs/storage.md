# Runtime Storage

Agent libOS stores durable runtime metadata through one `RuntimeStore`
contract. SQLite is the default local backend and PostgreSQL is the optional
multi-host database backend. A persistent database is still opened by only one
writable `Runtime` at a time; the lease described below is an application
invariant, not a replacement for SQL transaction isolation.

Ordinary Object Memory payloads are the main exception to SQL durability. The
object row contains a runtime-memory marker while the live payload is held in
the store's in-process payload cache. Checkpoints and image artifacts explicitly
persist bounded payload snapshots. A live marker that cannot be reconstructed
on reopen is released fail-closed rather than interpreted as user data.

LLM call records, pending-action generations, context-generation markers, and
eligible Responses tool outputs are durable SQL records. By default,
`llm.persist_full_io=true` retains complete prompts, visible tools, outputs,
tool calls, reasoning, raw provider payloads, and complete conditional-release
prepared requests. With `persist_full_io=false`, sensitive fields are replaced
by bounded previews, hashes, and resume metadata; an approved conditional
release can resume only while its hash-bound request remains in the same
executor's memory, and loss of that request fails closed before provider
dispatch. Provider-side response state is not in this database; the runtime
continues an opt-in chain only after validating these local rows against the
current profile, scope, and credential-keyed non-secret provider identity
fingerprint plus the current Sink/trust generation, clearance domain, Task
Authority manifest, and context epoch. Durable pending LLM actions also store
their metadata-only `DataFlowContext` so Human/message/child resume cannot lose
source labels.
`llm_context_generations` additionally stores a versioned, metadata-only label
high-water mark. Context preparation and compaction merge that row
monotonically before publishing a runtime-only context update, while
`LLMContextMemory.ensure()` seeds any replacement Object from it. During
upgrade/reopen, startup recovers a missing watermark from LIVE or RELEASED
process-state metadata before runtime-only payload recovery discards its
payload. It recognizes current and historical context-name prefixes as well as
durable `llm_context`/`prompt_cache` tags. SQLite and PostgreSQL use the same
validation and merge implementation.

Legacy pending-action and process-message carriers are canonicalized in
bounded, resumable migrations recorded in `storage_migrations`. Each migration
commits its cursor with the corresponding batch; a completed migration marker
skips rescanning historical rows on later opens. A marker version newer than
the running binary is rejected instead of rewriting future-format data.
A syntactically valid minimal
`DataFlowContext` is expanded to canonical defaults without changing its wait
state. Missing, partial, malformed, or otherwise invalid active pending context
is replaced with conservative secret/untrusted labels and marked for terminal
reconciliation; completed history receives only the conservative carrier.
Legacy message metadata is likewise canonicalized, preserving unrelated
fields, while incomplete labels discard an untrusted carrier id and become
conservative before delivery. Historical released-result Object metadata uses
a compatibility decoder: unknown ordered labels become
secret/untrusted/untrusted and invalid identity fields are reduced
conservatively, while structurally corrupt metadata still fails as a storage
validation error.

After Human, ObjectTask, Object Memory, and process callbacks are bound,
startup claims every invalidated pending action through a durable
invalidated/reconciling/reconciled state machine. For a non-terminal migrated
process, the in-store FAILED transition, reservation release, parent wakeup,
event, and audit commit together. An already-terminal row keeps its historical
status and result and completes only the missing budget/manager/finalizer
cleanup. Human/ObjectTask cancellation and the normal Object/Host terminal
finalizers run outside that transaction, preserving the result Object and
mergeable child memory until the parent adopts or discards it. A crash or
cleanup failure leaves `reconciling` so a later open retries cleanup without
replaying an exit event. One process failure does not prevent later migration
candidates from being attempted, although the open still reports the aggregated
error and fails closed.

Data-flow persistence uses the same SQLite/PostgreSQL repository contract:

- `sink_trust_registry` stores the active global generation;
- `sink_trust_records` retains versioned active/inactive rules and deterministic
  hashes;
- `data_flow_decisions` is append-only runtime evidence containing Sink,
  labels/source refs and hashes, outcome, reason, trust hash, and generation,
  never payload;
- `file_label_bindings` retains canonical path generations, content hash,
  labels, source refs, and tombstones;
- exact release bindings live in ordinary capability constraints and are
  abandoned/revoked with the normal finite-use lifecycle.

Current subtree label reads use an indexed, bytewise-collated prefix range and
bounded keyset batches on both backends; wildcard characters in path names do
not widen the tree. GUI snapshot event/audit reads similarly persist and index
a derived presentation-visibility flag, filter it before `LIMIT`, and backfill
legacy null flags in resumable batches. These derived flags affect bounded GUI
windows only, not the append-only evidence ledgers.

Registry replacement, generation advance, event, and audit publish in one
transaction. A data-flow decision, its event, and audit record likewise commit
together. The protected-operation prepare transaction rechecks generation and
source versions before reserving ordinary/release uses and inserting the
external-effect intent.

Explainable Operations adds three additive metadata tables shared by SQLite and
PostgreSQL: `operations`, deduplicated `operation_evidence`, and
`context_materialization_manifests`. They contain lifecycle state, causal ids,
typed evidence references, Object ids/versions, counts, and hashes, not an
additional copy of Object or prompt payloads. Older stores create these tables
on open; pre-existing audit/effect rows are not heuristically backfilled.

External-effect intents are also ordinary durable `external_effects` rows. A
primitive writes a pending intent, canonical argument hash, and idempotency key
before its provider boundary, then durably advances the transaction from
`prepared` to `dispatched`. Final classification uses one conditional update
to keep the same `effect_id`, move the transaction to `committed` or `unknown`,
and change structured `effect_state` from `pending` to `finalized`. The CAS also
matches pid, provider, operation, and target, so a duplicate or wrong-intent
settlement fails without adding a final row or changing unrelated evidence. A
finalization rollback therefore exposes the conservative pending row. Before
the provider call, a failed local precondition may conditionally abandon it;
after the call is attempted, only a provider-certified
`ProviderEffectNotStarted` path with no earlier information flow may delete it
without a final record. Core filesystem/clock/shell authority restoration and
intent abandonment share one transaction.

`authority_manifests` stores metadata-only launch contracts and their hashes.
`processes.model_tool_table_json` stores the model schema projection separately
from the complete callable image tool table. External effects have a unique
partial index on `(pid, idempotency_key)`. All fields are additive in the shared
SQLite/PostgreSQL schema contract.

These rows are append-only/versioned through runtime APIs, not cryptographically
tamper-proof. A database administrator can alter labels, trust, decisions, or
capabilities and is part of the Host trust boundary. Independent evidence
integrity requires external signed/append-only storage or attestation.

## Transaction Failure Semantics

Top-level store transactions use `BEGIN`/`COMMIT`; nested manager operations use
savepoints. Repository helpers do not commit independently while an outer
transaction is active, so a multi-manager lifecycle change can publish all of
its rows together. Transactions that mutate Object rows and the in-memory
payload cache opt into a payload snapshot and restore both SQL state and that
cache on rollback. A direct `set_object_payload` write also restores its prior
in-memory payload if its SQL write or commit fails; an uncommitted marker cannot
silently leave a newly visible runtime payload.

Hierarchical resource charging uses this boundary for the entire acting-process
to ancestor chain, child-reservation consumption, event, and audit. Overage
terminal callbacks run only after that transaction releases the store lock, so
Human/Object-task cleanup cannot invert the store lock order.

An exception in a transaction body rolls back the top-level transaction or
rolls back to and releases the nested savepoint. A failure while committing or
releasing a savepoint is also treated as an uncommitted operation: the store
attempts the matching rollback and restores any captured payload snapshot.

If that rollback or savepoint cleanup itself fails, the connection is no longer
trusted. The store closes and marks itself poisoned; later queries, mutations,
payload access, capability reservations, and claims fail closed with a storage
validation error. Callers must discard that `Runtime` and reopen from a healthy
database. Agent libOS never continues after guessing whether a partially
finalized transaction committed.

SQLite schema migrations for Object ownership/name/lifecycle rows run inside
transactions. Startup recognizes an interrupted `objects_old` rebuild, copies
only recoverable missing rows into a complete current table, and drops the old
table in the same transaction. An incomplete or structurally ambiguous source
fails closed instead of silently losing objects.

## Active-Runtime Leases

For file-backed SQLite, both the database connection and the lease name are
derived from the canonical resolved database path. Relative paths and symlink
aliases therefore cannot create independent writer identities for the same
database.

On POSIX systems with `fcntl` and `O_NOFOLLOW`, SQLite uses a sidecar
`<database>.runtime.lock` file opened with `O_NOFOLLOW` and `O_CLOEXEC` where
available. The runtime verifies that the opened descriptor is a regular file,
takes a non-blocking `flock`, then compares the descriptor's device/inode with
the still-unfollowed path before truncating or writing lease metadata. A
symlink, directory, replaced inode, or second lock holder is rejected without
modifying the unsafe target. The sidecar may remain after close; ownership is
the kernel lock, not file existence.

The database is atomically pre-created owner-only (`0600`) before SQLite opens
it. Existing owner-controlled database, lease, rollback-journal, WAL, and SHM
files are verified as regular files and tightened to `0600`; a file owned by a
different uid is rejected. This is required because the store may contain full
LLM prompts/outputs, capabilities, audit data, messages, and Human requests.
The Windows fallback relies on deployment ACLs rather than POSIX mode bits.

Where that secure file-lock path is unavailable, including the normal Windows
fallback, the connection uses SQLite `locking_mode=EXCLUSIVE` plus
`BEGIN EXCLUSIVE`. This kernel-managed database lease is crash-recoverable and
does not trust a stale create-once sidecar file.

PostgreSQL uses a session advisory lock. Its signed 64-bit key is a stable hash
of `current_database()` and `current_schema()`, so independent schemas do not
unnecessarily block one another while two runtimes targeting the same
database/schema pair cannot both open. Close unlocks the exact key acquired by
that connection; connection loss also releases the PostgreSQL session lock.

In-memory SQLite (`local` or `:memory:`) has no cross-process persistent-store
lease because each connection is a distinct store.

## Operational Consequences

- Do not open a GUI server and a writable CLI runtime against the same
  persistent target at the same time.
- A clean `Runtime.shutdown()` releases the lease. If shutdown reports active
  workers that could not drain, it deliberately leaves the store open; retry
  shutdown after those workers finish.
- A lease rejection means another runtime owns the target, or the lease path is
  unsafe. Deleting a lock sidecar is not a valid way to override a live
  `flock`.
- PostgreSQL credentials belong in environment-specific configuration. Lease
  diagnostics identify only the database/schema pair, not the DSN secret.
- Do not edit Sink trust, release, file-label, or decision rows directly. A
  supported registry mutation must advance generation and emit its coupled
  audit/event evidence; stale releases and provider chains depend on that
  generation.

See [CLI Reference](cli.md) for backend selection and
[Architecture](architecture.md) for the boundary between durable metadata,
in-memory payloads, append-only audit/finalized-effect records, and
CAS-transitioned pending effect intents.
