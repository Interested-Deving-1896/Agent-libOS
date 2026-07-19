# Runtime Storage

Agent libOS 0.3 stores durable runtime state through a `UnitOfWork` composed of
explicit domain boundaries, including `ProcessRepository`,
`ResourceRepository`, `RuntimePublicationRepository`,
`SnapshotCheckpointRepository`, `RuntimeModuleRepository`,
`PayloadRetentionRepository`,
`ObjectRepository`, `AuthorityRepository`, `EvidenceRepository`, and
`ExtensionRepository`. All repositories in one runtime share the same
transaction coordinator. Runtime code uses those repositories; the concrete
SQL store exposes only the Host transaction and backend implementation
boundary.

Process, resource-accounting, runtime-publication, operation/evidence,
module-publication, and Snapshot/Checkpoint persistence have explicit typed
Protocols and facade methods. Snapshot services exchange canonical `SnapshotRows` and
`ProcessSnapshot` aggregates with the repository; backend SQL, generic table
helpers, and Object-payload cache coordination stay behind that boundary. An
AST ratchet rejects raw SQL or generic table-helper regressions in the migrated
runtime services. Payload-retention scans and compare-and-swap reductions use a
separate typed repository so maintenance cannot regain the generic extension
facade or load full evidence history into Runtime services.

Those migrated repositories, including payload retention, also bind to
explicit backend Protocols. A
`UnitOfWork` validates concrete method presence and compatible positional and
keyword call shapes before it constructs any repository; dynamic
`__getattr__` shims and transaction-only backends therefore fail at assembly,
not on their first request. The architecture ratchet rejects `_delegate` and
`getattr` reflection inside the migrated repository classes. Legacy event,
audit, human/LLM, ObjectTask, message/rating, context-materialization,
external-effect, registry, and provider facades still use their reviewed
allowlists; they are not part of this typed vertical and must be migrated
explicitly before that compatibility mechanism can be removed.

SQLite is the default local engine. SQLite and PostgreSQL are independent
connection/dialect/lease adapters over the same typed repository
implementation and canonical 0.3 schema. The shared implementation emits a
small, documented SQLite-shaped SQL subset; the PostgreSQL dialect translates
that subset and is covered by a syntax-surface contract. PostgreSQL therefore
does not inherit the SQLite store or its connection/locking behavior, but it is
also not a second, copied repository implementation.

This release supports one writable Runtime per database/schema on either
backend. PostgreSQL's session advisory lease enforces that product boundary;
the current contract does not claim concurrent multi-Runtime writers or a
connection-pooled repository. Supporting those modes would require an explicit
database-level isolation design (for example row locks/epochs and corresponding
multi-connection tests), rather than relying on the in-process repository
lock.

## Strict 0.3 schema

Every 0.3 database contains a singleton `runtime_schema` row with schema
version `3`. Opening proceeds in this order:

1. Read the version marker and probe the required schema shape.
2. Reject an unsupported, unversioned, or incomplete store.
3. Only for an empty target, atomically create the complete schema and marker.

An interrupted bootstrap rolls back both schema and marker, so reopening the
same empty target retries initialization instead of misclassifying it as a 0.2
database.

The version-3 physical shape documented here is the release shape, including
typed process wait/outcome columns. It replaced draft version-3 shapes before
the 0.3 schema freeze; those development databases were never a supported
release format. The strict shape probe rejects them, and the runtime does not
present that rejection as a migration.

Text columns that form durable startup, recovery, or retention keysets have a
canonical bytewise collation: `BINARY` on SQLite and `"C"` on PostgreSQL. Both
the primary timestamp and the stable textual tie-breaker use that physical
shape, and their covering indexes use the same collation. Queries inherit the
validated column collation so SQLite retains composite row-value range seeks.
SQLite database files and PostgreSQL servers must both use UTF-8 encoding;
under UTF-8, `BINARY` and `"C"` ordering match Python's Unicode string ordering
for persisted cursor values. Opening an existing version-3 store fails closed
when its database encoding, any required keyset column, or any column collation
is not canonical; the column metadata itself is checked with one set-based
catalog probe. A UTF-16 SQLite file or locale-inheriting draft PostgreSQL schema
is therefore rejected rather than silently paginating with a different order
or degrading to a sort.

There are no migrations, backfills, or reconciliation paths from 0.2. A 0.2
database is archive-only and must be opened with an archived 0.2 release. The
0.3 runtime raises `UnsupportedStoreVersion` before it initializes tables or
changes rows. Checkpoints and checkpoint-derived Image artifacts are likewise
strictly versioned and rejected before operation evidence or process state is
written.

## Transaction model

Top-level UnitOfWork transactions use `BEGIN`/`COMMIT`; nested repository work
uses savepoints. Repository helpers never commit independently while an outer
transaction is active, so lifecycle changes can publish authority, process,
object, extension, audit, event, operation, and protected-effect rows as one
unit.

The PostgreSQL connection keeps connection-level autocommit enabled so a plain
read does not leave an implicit transaction open. Every repository mutation,
including a single-statement write, still enters an explicit outer transaction;
the lifecycle admission guard revalidates immediately before the real commit,
and rejection rolls that transaction back. SQLite uses the same mutation
helper and commit-guard contract.

When a Runtime is recovery-required, ordinary close/shutdown deliberately keeps
that exact admission guard and the SQLite/PostgreSQL active-runtime lease bound,
so no second writer can bypass the diagnostic fence. The explicit
`release_recovery_diagnostics()` handoff is the only no-write exit: after all
admissions and shutdown attempts are absent and transient workers have stopped,
it identity-matches the guard and closes the backend lease/store under the same
store lock. The store returns a structured ownership outcome: a failure while
the exact SQLite lease or PostgreSQL session is still owned restores the guard
and is retryable; a diagnostic raised after ownership is irreversibly released
never restores the stale guard, permanently disables the old store instance,
and completes the lifecycle handoff with warnings. It emits no
audit/event/terminal evidence and invokes no ordinary finalizers; only explicitly
registered no-write recovery cleanup may run. Only after an ownership-released
outcome may a newly opened Runtime take the same target and perform startup
recovery.

PostgreSQL handoff closes the owning session as its single release point; it
does not issue a separate `pg_advisory_unlock` first, because an ambiguous
unlock acknowledgement would make partial-close ownership unknowable. SQLite
closes the database connection before releasing its file lease, uses descriptor
close as the lease's single release point without a preceding `LOCK_UN`, and
probes the real driver handle after a close diagnostic. Builder-owned
failed-open cleanup atomically replaces the partial lifecycle guard with a
unique callable close reservation before graph teardown, so a stale cleanup
handle cannot close or yield the store to a successor Runtime.

Async close paths use two nonblocking store checks on the caller/event-loop
thread. `probe_admission_guard_close()` detects an active transaction, any
current-thread store lock scope, a lock held by another thread, or a stale
guard without changing state; lifecycle code uses it before teardown.
`claim_admission_guard_close()` repeats those exact checks and atomically marks
the guard close-pending immediately before worker offload. While claimed, new
transactions, `locked()` scopes, and dynamic identifier probes fail fast. A
pre-release backend failure that successfully restores the exact guard clears
the claim so diagnostics remain readable; retries must claim again. If guard
restoration itself is interrupted, the exact claim remains as the sole retry
token, blocks successor binding, and permits only that identity to probe,
re-claim, or finish releasing the backend. An
`OWNERSHIP_RELEASED` readiness result is terminal rather than retryable; the
exact release outcome clears the claim permanently with the guard, while a
stale caller may observe the terminal ownership fact without changing a live
guard.
Failed-open handles that legitimately need to repair an unbound owned guard use
`try_replace_admission_commit_guard()`, which applies the same nonblocking lock
and caller-scope checks and replaces only the exact expected owner; it cannot
take a guard installed by a successor Runtime.

Probe and claim return a structured `ownership_released` terminal outcome when
the backend lease/session is already gone. This is not treated as a retryable
lock failure: the exact lifecycle owner may finish graph teardown and call the
structured release operation to clear only its own stale guard. Ordinary async
close drains the off-loop release before reporting caller cancellation;
post-release warnings are retained by the lifecycle for idempotent readback.

Object payloads are runtime memory rather than ordinary SQL data. SQL Object
rows retain metadata and a live-payload marker. A transaction that changes
Object rows and payloads captures the in-memory payload state and restores both
layers on rollback. Checkpoints and Image artifacts explicitly serialize only
their bounded payload set.

If commit, savepoint release, rollback, or rollback cleanup leaves transaction
state uncertain, the store is poisoned and closed. Later access fails closed;
callers must discard the Runtime and reopen a healthy database. Exact ownership
controls remain available only when a separate backend lease is still held, as
with a file-backed SQLite store whose SQL connection was poisoned before its
file lease could be released. If the connection/session itself was the final
ownership point and is already gone, those controls report the structured
terminal result instead of publishing an impossible cleanup retry.

Optional `expected_states` arguments on repository compare-and-swap mutations
have one uniform meaning: `None` disables the state predicate, while an
explicitly supplied empty iterable matches no state and returns `False` without
changing durable state. Non-empty iterables retain the exact state fence.

## Durable authority and evidence

The shared schema durably stores capability state and reservations, Task
Authority manifests, process/resource state, Human and process-message waits,
LLM pending actions and context label history, registry state, explainable
operations, audits, events, and protected external-effect intents.

External effects use a prepared intent before provider dispatch. Settlement
conditionally advances that same row to committed, not-started, partial,
unknown, or other provider-classified outcomes. Capability-use reservations and
effect settlement share the enclosing transaction and preserve revoke-wins and
one-shot semantics.

An operation's runtime-publication binding is stored in a normalized nullable
column with a unique partial index, as well as in its versioned explanatory
metadata. The typed evidence repository performs exact indexed reverse lookup;
row decoding rejects a disagreement between the normalized column and metadata.
Publication planning and operation binding remain in one transaction, so the
index is an integrity constraint rather than a heuristic backfill.

Startup publication recovery uses typed, hard-bounded keyset pages over exact
kind, state, reconciliation marker, `created_at`, and publication id. Launch,
exec, and committed checkpoint-restore terminal-operation repair scans only
marker-false rows and exact-CAS marks completion; failed/manual checkpoint
restores remain forward-recovery inputs. RuntimeStore changes to a bound
operation atomically clear the marker, making the changed row eligible for
revalidation; direct database writes remain outside the application-integrity
boundary.

All mutation-capable startup recovery facades receive the lifecycle's bound
`require_recovery_lease` verifier, never its private token. Each facade and raw
backend invokes that verifier before its first durable read or transaction.
Consequently an `OPEN` runtime cannot manually scan, claim, reconcile, or
compensate startup work, and a same-shaped arbitrary ContextVar value cannot
impersonate the recovery lease. Recovery diagnostics are typed summaries with
exact totals and page-bounded samples rather than full-backlog lists.

Prepared protected-effect recovery runs before stale capability-use cleanup.
Once every valid prepared intent has restored its exact linked reservations,
the authority repository abandons remaining `reserved` rows through the
`(status, created_at, reservation_id)` keyset index. This ordering eliminates
the former startup-wide external-effect JSON scan and its unbounded protected
reservation set. Stale process executions similarly use a status/PID keyset
index; each page commits the PAUSED transition, concurrency high-water, audit,
and event evidence together while Runtime retains only a bounded PID sample.

Volatile Object payload cleanup is no longer a store-constructor side effect.
Runtime assembly invokes it under the opaque recovery lease, traverses the
partial `(created_at, oid)` recovery index in configured keyset pages, and uses
per-Object CAS writes to release metadata, links, and Object capabilities. It
runs before ObjectTask reconciliation, so succeeded tasks can deterministically
replace missing result references with `result_unavailable_after_reopen`.
Active tasks, missing results, and retryable terminal notifications each use
their own normalized, indexed `(created_at, task_id)` keyset scan. Runtime keeps
exact totals and at most one configured page of Object/ObjectTask identifiers.

Checkpoint-restore plans are complete at publication insert and carry an
immutable SHA-256 anchor in the publication receipt. Generic Host-visible
RuntimeStore methods reject checkpoint-restore insert, advance, recovery claim,
artifact append, plan update, and operation-reconciliation marking. An opaque
storage-owned writer is injected only into the restore reconciler; its state
machine enforces the main-commit marker, ordered phase/finalizer receipts,
recovery lease, failure classification, terminal receipt, and plan anchor.
Process-exec plans permit only effective no-op writes. The sole mutable
publication-plan slice is the exact
`boot_kind`/`materialized_workspace_root` pair for a planning or applying
process launch. Recovery verifies the checkpoint plan anchor before replaying
any reconciliation or durable-finalizer work, and terminal operation repair
also requires the complete causal transcript.

Orphaned `CREATED` processes use an indexed `NOT EXISTS`
launch-publication query. Neither path materializes complete publication or
process history.

Process tool tables also have a transactionally maintained normalized reverse
projection in `process_tool_bindings`. Publication compensation checks an exact
tool identity through `(tool_id, pid)` and reads durable tool existence through
the `tools` primary key; it never decodes every process or loads the complete
tools table. Candidate receipts bind the exact Object Memory descriptor OID,
so cleanup and convergence checks use candidate/descriptor primary keys. The
capability effect and its exact receipt share the publication UnitOfWork, so
recovery has no metadata-scan fallback for unreceipted capabilities.
`process_tool_bindings` is part of the complete fresh version-3 release shape,
not a lazy projection or startup backfill. A draft version-3 database that
lacks it is rejected by the strict shape probe; supported stores therefore
retain the projection across reopen without scanning or rewriting processes.
The projection also stores transactionally derived JIT eligibility. A
binary-collated partial covering index keyset-pages only eligible bindings, so
database work follows the JIT backlog rather than unrelated callable aliases;
the exact Tool/candidate bulk lookup remains the authority check for every
returned page.
Exec rollback and checkpoint pruning use the same projection for both callable
and model-only bindings. A single-process exclusion performs its reference
check and exact tool deletion in one transaction; a multi-process restore scope
streams only the indexed matching PIDs instead of materializing process rows.

Data-flow evidence stores labels, source references, hashes, Sink/trust
generation, and decisions—not payload copies. LLM pending actions and context
generations retain canonical metadata-only `DataFlowContext` values. All
required 0.3 columns are non-optional; malformed persisted state fails closed
instead of being repaired heuristically.

Process control state is persisted structurally. `wait_state_json` is a tagged
child/message/human/tool/pause/Host-resume wait, `outcome_json` is a tagged
exited/failed/killed outcome, and `state_generation` advances on every semantic
state transition. Normal runtime orchestration makes those transitions through
one `ProcessTransitionService`. The only explicit exceptions are typed
repository CAS primitives whose state update must be atomic with an execution
lease or snapshot restore; an exec-epoch commit additionally requires the exact
non-null admission token recorded by the matching applying `process_exec`
publication at its final pre-commit phase, then CASes RUNNING status,
generation, owner, and lease.
`status_message` is only a compatibility projection for older clients and is
never parsed as a live protocol. Wake tokens include the generation, so an
observer of an earlier wait cannot wake a later, textually identical wait after
an ABA cycle. Restore allocates a generation above the durable high-water mark,
while checkpoint fork resets the new process identity to generation zero and
remaps typed PID/Object references. Message-wait filters are strict JSON trees
with string object keys and finite numbers; no storage serialization may coerce
their identity. Checkpoint process rows require the physical JSON text values
(including the literal text `null`) rather than a SQL null for either tagged
column.
Public checkpoint-inspect projections strictly decode those tagged columns and
publish the canonical mappings with the snapshot `state_generation`; they never
derive control state from `status_message` or substitute current live state.
Generic process patch/update APIs reject `status`, `wait_state`, `outcome`, and
`state_generation` before writing. The transition repository primitive
revalidates the complete product type and CAS fence and computes
`state_generation + 1`; explicitly typed execution/restore CAS primitives own
the same generation increment at their atomic commit point. Callers cannot
supply or rewind the committed generation.

These records are append-only or versioned through Runtime APIs, but are not
cryptographically tamper-proof against the Host or a database administrator.
Independent integrity requires externally signed or append-only evidence.

## Active-runtime leases

A persistent target is owned by one writable Runtime at a time.

- File-backed SQLite canonicalizes the database path, secures database and
  sidecar files, and on POSIX uses a non-blocking `flock` over an
  `O_NOFOLLOW` sidecar. Where that mechanism is unavailable it uses SQLite
  exclusive locking.
- PostgreSQL uses a session advisory lock derived from the database/schema
  identity. Session close is the single ownership-release point; close never
  attempts a separate explicit advisory unlock. Cleanup failures are reported
  without replacing the primary failure, and connection loss releases the lock.
- In-memory SQLite has no cross-process lease because every connection is an
  independent store.

Do not open a GUI server and another writable CLI Runtime against the same
persistent target. Do not edit capability, trust, label, decision, or evidence
rows directly; supported mutations must publish their coupled generation and
audit/event evidence.

See [CLI Reference](cli.md) for backend selection and
[Architecture](architecture.md) for the authority, evidence, primitive, and
Runtime dependency boundaries.
