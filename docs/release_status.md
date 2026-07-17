# Agent libOS 0.3.0 Status

Agent libOS 0.3.0 is an integration candidate and is not ready for release.

## Current state

- The supported Skills, JIT, Image, Checkpoint, MCP, PTY, CLI, GUI,
  SQLite, and PostgreSQL surfaces remain available.
- `Runtime` is a host facade over explicit build, lifecycle, launch, image-boot,
  repository, snapshot, and execution services.
- Capability evaluation, finite-use leasing, and authority mutation are
  separate services with explicit audit, event, operation, and effect ports.
- Snapshot restore, fork, exec rollback, and checkpoint-derived images use
  explicit versioned snapshot and artifact contracts.
- Storage uses a strict schema-version marker. A 0.2 store or artifact is
  rejected before mutation and remains readable only with the archived 0.2
  release.
- Module registration rolls back through a per-module journal. Hooks receive
  explicit services and do not retain or snapshot `Runtime` internals.
- Public `Runtime.open`, top-level model exports, manifest formats, CLI
  behavior, and GUI behavior remain the supported compatibility boundary.

## Validation state

- Local compilation, security invariants, architecture checks,
  protected-operation checks, unit and targeted security tests, GUI tests,
  typecheck, and build pass.
- The combined deterministic Python matrix is not yet a release pass. A
  parallel all-lanes run had one shell timeout that passed in isolated and
  security-lane reruns.
- Fresh-store contracts are covered on SQLite; PostgreSQL remains a
  service-backed release gate.
- Known correctness work remains in lifecycle admission and shutdown, process
  concurrency and recovery, authority transactions, checkpoint and image
  compensation, payload rollback, and MCP resource accounting.
- Runtime-safety and practical-workflow evaluations remain validation surfaces;
  publication-level benchmark evidence is not yet a release claim.
- Version 0.3 source and wheel artifacts, scope validation, and isolated
  installation remain pending release gates.
- Native Windows PTY/process containment, desktop packaging, and real external
  providers remain platform- or provider-specific verification surfaces.
