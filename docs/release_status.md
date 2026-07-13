# Current Release Status

Updated: 2026-07-14 (Asia/Shanghai)

This is the living validation entrypoint. It records only checks actually run
against the current shared working tree; it does not inherit counts from the
historical prelaunch report.

## Snapshot identity

- Base Git commit: `a5cc726e1baf943534284aabd790681d5284b167`
- Working tree: **dirty**; the subsystem/documentation review is complete, but
  this development snapshot, including the GUI snapshot source-bounding work,
  is not a release candidate until it is committed and the applicable
  environment gates below are recorded.
- Frozen validation output: `/tmp/agent-libos-gui-snapshot-20260714-final`.
  Its `metadata.json` is the source for the exact
  `provenance.git.working_tree_sha256`. The digest is intentionally not copied
  into this tracked page: changing this page after hashing would change the
  dirty-tree digest. Preserve the out-of-tree output when packaging an artifact.
- Local validation platform: macOS 26.5.2/Darwin 25.5.0 arm64, CPython 3.11.15,
  Deno 2.8.1, Node 24.15.0, npm 11.15.0, and repository GUI dependencies.

## Verified on this working tree

| Gate | Result | Scope |
| --- | --- | --- |
| `tests/unit/test_test_matrix.py` | 18 passed | Hard process-tree lane timeout, including a real spawned descendant and the aggregate `all` lane, plus worker/default argument behavior |
| Configuration, GUI-schema, and documentation-contract unit tests | 7 passed | Exact config-field inventory, confirmed-route/schema drift, JSON Schema examples, 38 Markdown documents' local links/anchors, and the 26-command CLI reference |
| `tests/benchmarks/test_runtime_safety_benchmark.py` | 36 passed, 1 real-LLM test skipped | Runner interventions, fail-closed collection, and provenance-bearing metadata |
| MCP primitive/SDK/documentation focused run | 68 passed | Registry/authority behavior plus real local FastMCP stdio and Streamable HTTP, including pre-materialization stdio, JSON, SSE, and content-encoding bounds |
| Deterministic Python matrix | 1349 passed, 8 skipped | All six Python lanes with four workers, run outside the macOS sandbox with real Deno available; skips were six PostgreSQL nodes, one platform-inapplicable filesystem fallback, and one Windows PTY node |
| Python `compileall` | passed | `agent_libos`, tests, scripts, experiments, and benchmarks |
| Invariant manifest check | 53 invariants / 1369 collected nodes | Manifest references and pytest collection synchronized |
| Protected-operation static checker | passed | Provider effect lifecycle remains routed through the shared contract state machine |
| CLI/package validation | passed | Top-level and group help plus checked-in PTY Module, `swe-agent` Skill, and `mini-swe-agent` Image validation |
| GUI Vitest before build | 19 files / 64 tests passed | Source tests only; generated Electron output excluded |
| GUI TypeScript typecheck | passed | Web and Electron sources; production Electron config excludes test files |
| GUI production build | passed | Vite renderer plus cleaned Electron output |
| GUI Vitest after build | 19 files / 64 tests passed | Confirms `dist-electron` does not duplicate test discovery |
| 27-task `agent_libos_full` runtime-safety benchmark | 27/27 task success and safety pass; valid | 0/22 unauthorized performed effects, 0/22 allowed denials, zero unknown outcomes/classifications, no runner or infrastructure failures; provenance is in the frozen output above |

These are validation results for an intentionally dirty development snapshot,
not a claim that the remaining environment gates passed. A failed or
environment-blocked gate must remain explicit; do not replace it with an older
count.

## Not claimed by this snapshot

- GitHub Actions configuration includes PostgreSQL 17 integration, but this
  local ledger does not claim that remote workflow has run for the dirty tree.
- The optional MCP SDK was exercised against local FastMCP stdio and HTTP
  servers on this macOS host; remote proxy/TLS deployments and other operating
  systems remain provider/platform gates.
- Windows and Linux native release gates, real PostgreSQL service integration
  for this exact dirty tree, packaged Electron window smoke, and real LLM calls
  remain the environment gates described in
  [support_matrix.md](support_matrix.md).

## Update procedure

Before calling a commit a release candidate:

1. Record the final commit and `dirty: false`, or record the benchmark
   `provenance.git.working_tree_sha256` when reviewing an intentionally dirty
   artifact.
2. Run compileall, invariant sync, deterministic Python lanes, GUI lane, and a
   fresh provenance-bearing runtime-safety benchmark.
3. Record exact pass/skip/failure counts and output paths here; never copy them
   from another commit.
4. Run the environment gates required for every platform/provider advertised by
   that release.

The [2026-07-10 prelaunch report](prelaunch_hardening_report.md) remains useful
historical design and validation evidence, but is bound to its own commit and is
not a substitute for this ledger.
