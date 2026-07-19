# Runtime-publication reopen scale benchmark

This benchmark seeds 10,000 terminal publications, including a 1,001-row
unreconciled launch backlog with canonical durable operation bindings that
spans three configured pages, then reopens a real file-backed Runtime. It
also seeds a `2 * page_size + 5` completed checkpoint-payload delivery
attempt (1,005 rows in the CI profile) and
10,000 acked/aborted historical delivery-attempt control rows. The completed
payload rows are driven through completed-to-pending compensation,
pending-to-confirmed preparation, confirmed-to-pending compensation, and a
final pending-to-confirmed-to-completed delivery. Each of the five transitions
uses the production keyset helper, retains only a scalar cursor, and commits
one page-sized transaction. The preparing-attempt startup query must fetch no
historical control rows. Empty-attempt ACK and bound-attempt abort are rejected;
valid plus stale ACK and abort calls exercise the opaque control writer, exact
control-row CAS predicates, and same-transaction primary-key readbacks.

The schema inventory pins the global and attempt-keyset payload indexes, the
separate ACK/abort guard index, and both delivery-attempt control indexes,
including exact columns, partial predicates, and uniqueness. Query plans must
use the global pending index, the attempt-bound delivery index, the
preparing-attempt state index, the guard index, and the control-row primary key
with no temporary sort or history scan. Every payload page may fetch at most
one look-ahead row, so total raw rows and query counts remain page-proportional
rather than materializing the full ID backlog.

The benchmark
default-denies every unexpected publication statement; the only reviewed reads
are recovery pages, operation-reconciliation pages, exact-primary-key reads,
the domain check, the orphan anti-join, payload keyset pages, and exact
delivery-attempt readbacks. Exact CAS and invalidation writes are counted
separately. Each shape is anchored end to end after SQL
literal normalization, so a reviewed marker or predicate cannot hide an
additional clause. Table-reference detection covers SQLite's unquoted,
double-quoted, backtick-quoted, bracket-quoted, single-quoted, schema-qualified,
and no-space alias forms by fail-closing on the fixed table identifier.
Connection tracing starts on the independent read-only preflight connection and
the main connection in the connection factory, before an outer `connect`
wrapper can issue prefix SQL, then remains active through complete Runtime
assembly. It therefore covers direct cursor SQL as well as repository query
helpers. Every repository helper call must correspond to exactly one matching
traced SELECT, and the traced/helper shape multisets must be identical. The
benchmark captures the exact SQL and parameters issued by
`Runtime.open()`, explains both its initial and resumed tuple-keyset pages, and
fails if the exact composite-index columns, partial domain predicate, search
constraints, or no-sort plan contract changes.

The real handler must reconcile the exact backlog, expose no more than one page
of sampled IDs, issue exactly one query per pending page, fetch only the pending
rows plus one look-ahead row per non-final page, leave no marker outstanding,
and never fetch more than the configured page bound in any reviewed query. SQL
aggregation then proves the complete seeded publication terminal multiset still
contains every original id with its exact kind, owner, state, phase, plan,
receipt, error, and reconciliation marker. Every bound operation reaches the
canonical succeeded terminal outcome, including its expected evidence-role
set and completion marker. It also rejects any extra operation outside the
exact seeded binding set, without materializing history in Python.

Elapsed seed and reopen times are diagnostic only. Structural query and row
bounds are the release gate.

```console
uv run python experiments/run_publication_reconciliation_scale.py --profile ci
```
