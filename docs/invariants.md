# Agent libOS Runtime Invariants

The machine-checked runtime invariant map lives in
`tests/invariants.yaml`. It is the authoritative source for connecting safety
claims to pytest node ids and benchmark attack classes.

Validate it with:

```bash
uv run python scripts/check_test_invariants.py
```

The checker accepts JSON-subset or YAML syntax and fails when a listed pytest
node cannot be collected, an invariant lacks deterministic regression coverage,
an invariant's `benchmark_attack_classes` declaration diverges from the
top-level mapping, or a runtime-safety benchmark task uses an unmapped
`attack_class`.

## Current Invariant Groups

- `tool-visibility-is-not-authority`: visible tools and endpoints do not grant
  protected resource authority.
- `primitive-checks-before-effects`: primitives enforce capability, policy,
  approval, and validation before side effects.
- `capability-matching-and-delegation`: typed matching, deny dominance,
  one-shot grants, revocation, and delegation attenuation.
- `process-authority-is-explicit`: spawn, fork, exec, and cwd behavior do not
  imply broader authority.
- `object-memory-names-are-not-capabilities`: Object Memory names and
  namespaces do not bypass object capabilities.
- `human-approval-is-blocking-and-audited`: human questions and approvals block,
  resume, consume one-shot grants, and route through primitives.
- `shell-and-jit-containment`: shell and Deno JIT execution stay policy-bound,
  sandboxed, process-local, and syscall-mediated.
- `command-risk-rules-are-deterministic`: command risk rules separate
  harmless, risky, and destructive shell operations without model judgment.
- `sandbox-profile-derived-from-capability-decision`: primitive sandbox
  profiles are derived from the same capability decision that authorizes the
  operation.
- `tool-observability-redacts-sensitive-payloads`: tool audit/event
  observability stores bounded preview, hash, size, and truncation metadata
  instead of raw sensitive args or results.
- `jit-security-does-not-rely-on-static-blacklist`: JIT safety is enforced by
  Deno no-permission isolation, libOS syscalls, capabilities, human approval,
  and budgets rather than dangerous API regex blacklists.
- `tool-policy-cannot-self-grant-authority`: ToolPolicy declarations cannot
  grant execution, resource authority, or confirmation.
- `resource-budgets-are-hierarchical`: resource usage is charged to the acting
  process and its parent chain, and visibility/capability mechanisms cannot
  mint additional budget.
- `llm-token-usage-is-charged-before-tool-dispatch`: provider-reported LLM token
  usage is settled before any model-selected tool call is dispatched.
- `subprocess-resource-profiles-are-enforced`: shell and Deno subprocess wall,
  CPU, and RSS limits are enforced by providers and audited on exceedance.
- `skill-activation-does-not-grant-authority`: Skills change visibility and
  prompt context without granting resources.
- `checkpoint-restore-and-fork-are-scoped`: checkpoint restore/fork are scoped,
  capability-controlled, and append-only outside reconstructable state.
- `image-self-evolution-requires-image-authority`: image registration, package
  boot, exec, and checkpoint commit require image authority and do not bake
  external authority.
- `jsonrpc-provider-effects-are-registered-and-classified`: JSON-RPC calls use
  registered endpoint/method authority and classified provider effects.
- `runtime-safety-benchmark-is-deterministic`: benchmark tasks and smoke runs
  remain deterministic and token-free by default.

## Known Test Gaps

- Audit explain is not implemented yet; current tests check audit record
  emission and selected audit counts, not query/explanation completeness.
- The runtime-safety benchmark is an early deterministic workload, not a
  complete paper evaluation suite.
- Context materialization metadata is not complete enough to compute
  included/omitted/summarized/truncated object statistics for every LLM call.
- Real MCP, Git worktree, and mock PR providers are planned but not implemented.
