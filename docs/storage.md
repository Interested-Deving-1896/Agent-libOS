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
eligible full-I/O Responses tool outputs are durable SQL metadata. Provider-side
response state is not in this database; the runtime continues an opt-in chain
only after validating these local rows against the current profile, scope, and
credential-keyed non-secret provider identity fingerprint.

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

See [CLI Reference](cli.md) for backend selection and
[Architecture](architecture.md) for the boundary between durable metadata,
in-memory payloads, append-only audit/finalized-effect records, and
CAS-transitioned pending effect intents.
