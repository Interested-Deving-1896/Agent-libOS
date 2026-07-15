# Agent libOS 0.2.0 Status

Agent libOS 0.2.0 is in pre-release validation.

## Current status

- Version identifiers are aligned across the Python package, project metadata,
  lockfile, and GUI package.
- Local documentation links and anchors resolve successfully.
- POSIX Shell and PTY executable snapshots preserve explicit virtual-
  environment launcher semantics while execution remains pinned to the
  verified snapshot.
- PTY module source verification and the default trust configuration agree.

## Validation

| Area | Status |
| --- | --- |
| Unit tests | 179 passed |
| Shell/PTY and documentation regressions | 7 passed |
| Static and integrity checks | compileall, invariant synchronization, protected-operation coverage, module verification, CLI smoke, lockfile, and diff checks passed |
| Security tests | Blocked by five data-flow label-integrity regressions |
| Provider and runtime suites | Full rerun required after the data-flow regressions are corrected |

The version is not ready for release until the security regressions are fixed
and the affected suites pass together.

## Remaining validation boundaries

- Windows and Linux native packaging.
- Real PostgreSQL integration.
- Packaged Electron window smoke testing.
- Remote proxy/TLS deployments and real LLM calls.

See [support_matrix.md](support_matrix.md) for provider and platform coverage.
