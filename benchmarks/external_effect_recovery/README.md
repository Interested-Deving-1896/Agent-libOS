# External-effect recovery scale benchmark

This benchmark populates a large file-backed SQLite history, closes the writer,
reopens the persisted runtime store, first probes the typed
`ExternalEffectRecoveryQuery`, and then assembles a real `Runtime` over that
store so `ProtectedOperationSDK.recover_prepared()` processes the backlog. It
fails on structural regressions: query work must remain proportional to pending
pages, raw rows may include only one look-ahead row per non-final page, initial
and resumed queries must use the matching recovery index, the handler must
leave no pending prepared rows, and Runtime diagnostics may retain at most one
page of effect IDs. A guarded legacy-list method also makes the benchmark fail
if startup falls back to loading complete external-effect history. Connection
tracing covers the read-only preflight connection, the main connection from its
creation through full schema initialization, and the complete Runtime handler
window, including statements issued through `_query`, `_execute`, the
connection, or a direct cursor. The trace is installed by the connection
factory, before an outer `connect` wrapper can issue prefix SQL. Exact
per-shape ledgers default-deny unexpected
SELECTs and DML; only the reviewed tuple-keyset page, effect-primary-key,
set-based stale-operation index, and identity-fenced prepared-effect deletion
shapes are recognized. The gate also verifies the exact recovery-index columns
and search constraints and uses SQL aggregation to prove every expected seeded
identity reaches its expected final classification without materializing the
history in Python.

Elapsed seed and recovery times are recorded for diagnostics only. They are
never pass/fail thresholds, so slow or noisy CI hosts do not create brittle
results.

Run the 100k-record CI profile:

```console
uv run python experiments/run_external_effect_recovery_scale.py --profile ci
```

Run the one-million-record manual/nightly profile:

```console
uv run python experiments/run_external_effect_recovery_scale.py --profile million
```
