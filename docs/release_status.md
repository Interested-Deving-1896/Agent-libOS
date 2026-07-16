# Agent libOS 0.2.1 Status

Agent libOS 0.2.1 is ready for release within its documented core Python wheel
and Python source-distribution scope.

## Current status

- Version identifiers are aligned across the Python package, project metadata,
  lockfile, GUI package, and GUI lockfile.
- The core wheel and Python source distribution build successfully and conform
  to their declared content boundary.
- The wheel installs in an isolated environment; both console entrypoints and
  the deterministic demo run outside the source checkout.
- Deterministic runtime, security, self-evolution, provider, benchmark, and
  documentation checks pass together.
- GUI unit tests, type checking, and production compilation pass together.
- PTY module source verification and the default trust configuration agree.

## Validation

| Area | Status |
| --- | --- |
| Deterministic Python matrix | 1648 passed; 26 environment-specific cases skipped |
| GUI | 67 passed; type checking and production build passed |
| Release artifacts | Wheel and source distribution validated; isolated install and entrypoint/demo smoke passed |
| Runtime-safety benchmark | 28/28 task success and 28/28 safety pass; no unknown outcomes or classifications |
| Practical workflows | 3 native-live and 80 modeled scenarios passed; no modeled fallback |
| Static and integrity checks | Compile, 57-invariant synchronization across 1686 test nodes, protected-operation coverage, module verification, lockfile, documentation, and diff checks passed |

The core Python wheel contains the runtime package and its console entrypoints.
The Python source distribution additionally contains the repository-level PTY
module, example Skill and Image packages, benchmarks, tests, and documentation.
Electron sources remain repository-checkout assets validated by the GUI lane.

## Conditional environment boundaries

- PostgreSQL support requires the service-backed integration job.
- Windows PTY, process-tree containment, and native packaging require Windows
  validation.
- Native desktop packaging and the packaged Electron window require a desktop
  release-gate run.
- Real MCP servers, remote proxy/TLS deployments, and real LLM calls remain
  opt-in environment gates.

These boundaries do not broaden the validated core release scope. See
[support_matrix.md](support_matrix.md) before making platform- or
provider-specific support claims.
