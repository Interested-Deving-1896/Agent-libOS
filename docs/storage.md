# Runtime Storage

Agent libOS 0.3 stores durable runtime state through a `UnitOfWork` composed of
five domain boundaries: `ProcessRepository`, `ObjectRepository`,
`AuthorityRepository`, `EvidenceRepository`, and `ExtensionRepository`. All
repositories in one runtime share the same transaction coordinator. Runtime
code uses those repositories; the concrete SQL store exposes only the host
transaction and identifier-validation boundary.

SQLite is the default local engine. PostgreSQL is an independent engine for a
service-backed deployment; it does not inherit SQLite behavior. The engines
share repository semantics and the canonical 0.3 schema while retaining their
own connection, dialect, and lease implementations.

## Strict 0.3 schema

Every 0.3 database contains a singleton `runtime_schema` row with schema
version `3`. Opening proceeds in this order:

1. Read the version marker and probe the required schema shape.
2. Reject an unsupported, unversioned, or incomplete store.
3. Only for an empty target, atomically create the complete schema and marker.

An interrupted bootstrap rolls back both schema and marker, so reopening the
same empty target retries initialization instead of misclassifying it as a 0.2
database.

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

Object payloads are runtime memory rather than ordinary SQL data. SQL Object
rows retain metadata and a live-payload marker. A transaction that changes
Object rows and payloads captures the in-memory payload state and restores both
layers on rollback. Checkpoints and Image artifacts explicitly serialize only
their bounded payload set.

If commit, savepoint release, rollback, or rollback cleanup leaves transaction
state uncertain, the store is poisoned and closed. Later access fails closed;
callers must discard the Runtime and reopen a healthy database.

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

Data-flow evidence stores labels, source references, hashes, Sink/trust
generation, and decisions—not payload copies. LLM pending actions and context
generations retain canonical metadata-only `DataFlowContext` values. All
required 0.3 columns are non-optional; malformed persisted state fails closed
instead of being repaired heuristically.

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
  identity. Connection loss releases the lock.
- In-memory SQLite has no cross-process lease because every connection is an
  independent store.

Do not open a GUI server and another writable CLI Runtime against the same
persistent target. Do not edit capability, trust, label, decision, or evidence
rows directly; supported mutations must publish their coupled generation and
audit/event evidence.

See [CLI Reference](cli.md) for backend selection and
[Architecture](architecture.md) for the authority, evidence, primitive, and
Runtime dependency boundaries.
