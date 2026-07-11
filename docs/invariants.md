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
  approval, and validation before side effects, including hidden provider
  metadata gates, filesystem mutation authority before target-state
  observation, stale-size-safe bounded reads, PTY spawn cleanup, and write
  limits. Filesystem/clock/shell/PTY provider calls persist a pending `unknown`
  effect intent before the boundary, CAS the same id on final classification,
  and after attempting the call remove it without a final record only when the
  provider certifies `ProviderEffectNotStarted` and no earlier information flow
  occurred. Clock sleep/asleep inserts the intent before its first monotonic
  observation, treats elapsed-time measurement as information flow, and permits
  restore/abandon only when that first observation is certified not-started.
- `capability-matching-and-delegation`: typed matching, deny dominance,
  one-shot grants, atomic default consumption, exact effect reservations,
  revoke-wins restoration, crash abandonment, grant-as-transfer,
  parent-linked delegation attenuation, restrictive parent boundaries,
  malformed authority-rule fail-closed behavior, and ISO-normalized leases.
- `process-authority-is-explicit`: spawn, fork, exec, and cwd behavior do not
  imply broader authority. Cwd selection requires filesystem directory read,
  and explicit child/PTY cwd probes occur only after their higher-level
  authority gates and under a filesystem effect intent.
- `task-authority-manifest-bounds-launch`: image requirements are declarations;
  Host manifests compile launch grants and bound model requests, child
  transitions, budgets, approval policy, and provider effect classes.
- `effect-transactions-are-idempotent-and-reconcilable`: provider intents bind
  canonical arguments and idempotency keys, approval leases bind exact effects,
  and startup reconciliation queries but never replays providers.
- `data-labels-propagate-conservatively`: derived Object sensitivity, trust,
  and integrity labels merge conservatively; manifests expose metadata only;
  label downgrade requires declassification authority.
- `model-tool-projection-does-not-change-authority`: lazy model schemas are a
  durable projection of the complete image tool table and cannot add
  capabilities.
- `object-memory-names-are-not-capabilities`: Object Memory names and
  namespaces do not bypass object capabilities. Successful namespace listing
  consumes every finite namespace/object visibility decision used in the
  returned result in the same transaction as its audit.
- `object-memory-materialization-budget-is-authoritative`: Object Memory
  context materialization is bounded by final rendered object text, not trusted
  metadata token estimates.
- `context-compaction-preserves-authority-and-fails-closed`: context compaction
  uses child-process summarizers without granting external authority, validates
  summaries, preserves process authority, and fails closed on races or invalid
  output.
- `child-memory-merge-lifecycle-is-explicit`: terminal child process memory
  remains mergeable, then is adopted or released by the parent lifecycle.
- `object-memory-lifecycle-is-explicit`: Object Memory ownership, release, and
  RAII cleanup are explicit and revoke stale authority, including Object-bound
  PTY handles. Lifecycle mutations serialize ownership-lock before store
  transaction and use LIVE, owner, and version conditional writes so release
  races, concurrent updates, and owner ABA fail closed. Trusted delete rolls
  back Object release, capability revocation, audit, and in-memory payload
  together; a multi-Object ownership transfer and its audit are all-or-nothing.
- `runtime-store-single-active-writer`: a writable persistent runtime store has
  at most one active Runtime opener across canonical/symlink path aliases.
  SQLite validates a no-follow regular-file lease or uses its kernel exclusive
  fallback; the secure POSIX path keeps database/lease/journal/WAL/SHM files
  owner-only; PostgreSQL keys advisory leases by database/schema.
- `storage-transactions-recover-or-fail-closed`: commit/savepoint finalization
  failure restores SQL and opted-in Object payload state; rollback failure
  poisons/closes the store, and interrupted Object schema rebuilds recover
  atomically or fail closed.
- `human-approval-is-blocking-and-audited`: human questions and approvals block,
  resume, reserve and consume one-shot grants exactly once, are decided exactly
  once from pending state, and route through primitives. Concurrent terminal
  drains serialize request selection through the terminal transition, so only
  the winning worker may install an automatic permission policy or cross the
  human output provider boundary. Permission policy and question-answer types
  are explicit, run-local `ContextVar` policy cannot cross concurrent runs,
  multiple blockers remain waiting, and terminal processes cancel requests.
  Blocking terminal provider I/O runs outside the selection lock so exit/cancel
  never waits for human input, and GUI history bounds never omit pending rows.
  Human output commits delivered state/event/audit/pending intent before the
  provider and remains at-most-once after provider or classifier failure.
  Terminal prompt reads and automatic-response writes also use structured
  pending intents; they retain only length/hash observations, never raw
  prompt, answer, or provider exception text. Human output provider failures
  likewise retain only the error type.
- `shell-and-jit-containment`: shell and Deno JIT execution stay policy-bound,
  including shell policy capability effects and finite-use leases, sandboxed,
  process-local, cached-only at runtime, and syscall-mediated. JIT lifecycle
  rows/aliases/handles commit atomically, composite failures discard unpublished
  candidates, and cancellation terminates the isolated Deno process group;
  a dedicated POSIX death-pipe/process-group supervisor or Windows
  `KILL_ON_JOB_CLOSE` Job Object establishes hard-host-termination containment
  before Deno is released, failing closed if containment setup fails;
  PTY creation reuses shell authorization and follow-on PTY access uses Object
  capabilities. Shell and PTY reserve finite-use authority, restore only on
  certified `ProviderEffectNotStarted`, and record ambiguous failures as
  `unknown`. PTY spawn/write/resize/close use structured pending intents and
  same-id conditional finalization; spawn publication failures retain cleanup
  metadata, while classifier failures finalize unknown evidence rather than
  dropping it. Follow-on finite object rights reserve/restore around the host
  call, and automatic child-exit cleanup records a close intent before exit-code
  observation/close. Object release finalizers run outside the SQL transaction so PTY
  close can durably record its intent; `swe_edit` refuses truncated source.
  Auto-allowed Git inspection disables optional locks, repository fsmonitor,
  and external diff helpers before the provider boundary.
- `command-risk-rules-are-deterministic`: command risk rules separate
  harmless, risky, and destructive shell operations without model judgment.
- `sandbox-profile-derived-from-capability-decision`: primitive sandbox
  profiles are derived from the same capability decision that authorizes the
  operation.
- `audit-query-windows-retain-latest-records`: limited audit queries select the
  latest matching records before returning them chronologically, and process
  audit views filter before applying their limit.
- `tool-observability-redacts-sensitive-payloads`: tool audit/event
  observability stores bounded preview, hash, size, and truncation metadata
  instead of raw sensitive args or results.
- `jit-security-does-not-rely-on-static-blacklist`: JIT safety is enforced by
  Deno no-permission isolation, libOS syscalls, capabilities, human approval,
  and budgets rather than dangerous API regex blacklists.
- `tool-policy-cannot-self-grant-authority`: ToolPolicy declarations cannot
  grant execution, resource authority, or confirmation.
- `tool-result-size-boundary-is-explicit`: tool result payload limits prevent
  unbounded result persistence while preserving committed side effects as
  explicit omitted-success results instead of retryable failures.
- `workflow-entry-uses-toolbroker-authority`: user-facing workflow entrypoints
  run tools through process tool tables, ToolBroker, result objects, and normal
  wait/exit/exec lifecycle semantics.
- `process-message-waits-are-race-free`: an empty blocking mailbox read and its
  wait registration are atomic with message posting, so a concurrent matching
  post either satisfies the read or wakes the registered process. Message row,
  evidence, terminal recheck, and wake state also roll back together.
- `runtime-lifecycle-transitions-are-atomic`: capability issuance, the core
  process exec row/capability/evidence transition, process exit, and
  parent/message waiter transitions do not publish partial authoritative state
  when an in-transaction sink fails. Higher-level image boot uses compensating
  restore rather than claiming one transaction across host/package work.
- `scheduler-quantum-ownership-is-serialized`: scheduler and direct pid
  single-step APIs share the same runtime lock, store claim, and resource-charge
  boundary, so one process cannot be re-entered concurrently.
- `runtime-shutdown-is-drained-and-retry-safe`: scheduler work, ObjectTask
  executors, PTY reader/monitor workers, and GUI runtime users drain before
  shared state closes; a timed-out shutdown leaves storage open and can be
  retried.
- `object-task-entry-uses-toolbroker-and-object-authority`: Object-bound
  background tasks run tools through ToolBroker, process tool tables, Object
  capabilities, owner-watch Object Memory primitive notifications, and
  process-message boundaries. Runner processes are host-managed and excluded
  from the LLM scheduler; one-shot owner authority is reserved before runner
  creation and committed with the durable task record. The mapped cross-actor
  regression directly proves finite read consumption for get and finite write
  consumption for cancel. Failed executor handoff
  terminalizes the task and removes the unstarted runner, while failed result
  wiring terminalizes the runner and releases the unpublished result and its
  derived handles. Terminal/cancel reconciliation must not leave active pins
  behind, and owner-watch resumes only replay tools with explicitly safe
  message-receive semantics.
- `llm-call-records-opt-out-are-bounded-and-redacted`: when
  `llm.persist_full_io` is false, LLM call persistence stores bounded preview,
  size, hash, and truncation metadata instead of raw prompts, tool arguments,
  reasoning, or provider responses.
- `llm-responses-state-chain-is-lossless`: OpenAI Responses state chaining
  is opt-in and preserves every representable native tool output, including
  parallel batches and reopened waits. Missing/extra/redacted outputs, changed
  profile/scope/context generation, or local rollback break the chain rather
  than continuing against guessed provider state.
- `llm-async-clients-are-event-loop-scoped`: real async SDK clients and their
  keep-alive pools are request-scoped and cannot cross scheduler event loops.
- `llm-provider-state-is-scope-bound-and-nonreplayable`: provider-chain state
  is bound to process/context plus a credential-keyed model, endpoint, API-mode,
  and tenant fingerprint. Durable waits use token-scoped
  pending/resuming/completed CAS and synchronize restored generations; an ABA
  claim, post-claim exception, or interrupted reopen fails closed and is never
  auto-replayed.
- `llm-profile-selection-is-process-local`: host-selected LLM profiles are
  stored as process-local ids, resolved at LLM-call time, inherited by child
  processes, preserved by image-package defaults, isolated from non-default
  ambient provider environment, and fail closed when the id is unknown.
- `resource-budgets-are-hierarchical`: resource usage is charged to the acting
  process and its parent chain, and visibility/capability mechanisms cannot
  mint additional budget. The complete hierarchy, reservations, event, and
  audit commit atomically; overage terminal callbacks run after releasing the
  store lock. Discrete counts/bytes/tokens are integers while runtime and
  subprocess wall/CPU seconds are continuous finite values.
- `llm-token-usage-is-charged-before-tool-dispatch`: provider-reported LLM token
  usage is validated (including type, sign, and component consistency) and
  settled before any model-selected tool call is dispatched.
- `subprocess-resource-profiles-are-enforced`: shell and Deno subprocess wall,
  CPU, and RSS limits are enforced by providers and audited on exceedance; PTY
  supervision runs independently from output reads, accumulates observed CPU by
  process identity, and fails closed when process-tree accounting is denied.
  Cleanup falls back to explicit descendant signaling when process-group
  signaling is denied and serializes concurrent close attempts.
- `skill-activation-does-not-grant-authority`: Skills change visibility and
  prompt context without granting resources; API actor mode must still honor
  skill capability or human-approval gates. Finite-use Skill permissions are
  reserved before a registry, trust, activation, or unload mutation and are
  committed only with that mutation. Registry/trust/audit state changes are
  transactional; failed activation cannot leave a visible JIT alias, and
  reactivation or unload retires the exact superseded process-local JIT rows.
- `runtime-modules-load-trusted-code-atomically`: startup Runtime Modules bind
  trust to the current source hash, reject ambiguous manifests and duplicate
  module ids, resolve import strings without executing untrusted package code,
  bound source hashing, and roll back failed declared or hook-created
  tool/image/syscall/hook registrations so persisted module status stays
  aligned with loaded runtime state.
- `checkpoint-restore-and-fork-are-scoped`: checkpoint creation atomically
  captures one consistent store snapshot and publishes its row, head, initial
  read capability, event, and audit. Restore/fork are scoped,
  capability-controlled, ownership-based, revoke-wins, and append-only outside
  reconstructable state. Borrowed/`EXTERNAL_REF` state is not cloned, JIT
  tool/candidate ids are remapped and atomically published, and finite-use
  snapshot authority is never copied. Fork revalidates/consumes actor authority
  only in its publication transaction, global Skill/Image rows are not replaced
  by fork, and post-commit failures are reported without claiming rollback.
  The implementation uses one reservation scope for diagnostic
  inspect/diff/replay; the mapped one-shot regression directly proves inspect
  consumes the selected finite checkpoint/process read exactly once.
- `image-self-evolution-requires-image-authority`: image registration, package
  boot, exec, and checkpoint commit require image authority and do not bake
  external authority. Failed registration/commit removes new artifacts and
  restores replaced manifests; failed package boot/exec removes private
  workspace and unpublished JIT source/candidate state. Registry callers and
  getters receive isolated deep copies, concurrent same-id registrations are
  serialized and revalidated in the cache/store critical section, and
  committed-image boot does not overwrite the global Skill registry.
- `agent-output-is-not-control-channel`: untrusted command output cannot trigger
  lifecycle control actions; submission/exit must use explicit tool or syscall
  arguments.
- `jsonrpc-provider-effects-are-registered-and-classified`: JSON-RPC endpoint
  registration and calls use registered endpoint/method authority, gate calls
  and per-item registry operations before manifest metadata is exposed, and
  classify provider effects. Registry row/stale-grant/event/audit mutations are
  transactional. A call reserves finite authority and persists pending evidence
  before non-local DNS and transport; DNS observation prevents later transport
  PENS from erasing information flow or restoring the use.
- `mcp-provider-effects-are-registered-and-classified`: MCP server registration
  and tool calls use registered server/tool authority, gate calls before server
  metadata is exposed, and classify provider effects. Registry mutations are
  transactional. Refreshed tool listing/tool calls atomically reserve their
  deduplicated main, server, process-spawn, and exact stdio authority and persist
  pending evidence before DNS/live-provider boundaries. Local/stdio first-call
  PENS may restore; non-local DNS observation cannot be erased.
- `explainable-operations-use-explicit-causality`: protected LLM, Tool,
  syscall, primitive, and runtime boundaries persist typed parent/child rows and
  explicit evidence links. Human/child/message waits reuse their durable
  operation ids; reopen interrupts only orphaned running rows. Explanation
  completeness checks declared roles and never fills gaps from pid/time
  proximity. Host output applies observability redaction.
- `context-manifests-are-metadata-only`: each LLM context preparation records
  source Object selection/omission reason, version, transform, tokens, hashes,
  final context generation/Object, and compaction metadata without copying
  Object payloads or rendered prompt text.
- `runtime-safety-benchmark-is-deterministic`: benchmark tasks and smoke runs
  remain schema-v1, deterministic, and token-free by default. Effect outcome
  and evidence are explicit; exact/prefix/glob matching cannot broaden
  implicitly; unknown/invalid/orphan/runner-failure output nulls rate fields;
  false-denial and unauthorized-effect denominators use only their documented
  qualified effect subsets.
- `practical-native-evidence-has-no-modeled-fallback`: native practical rows
  require real tool, provider-state, external-effect, and operation evidence;
  modeled rows stay in a separate denominator.

## Known Test Gaps

- The runtime-safety benchmark is an early deterministic workload, not a
  complete paper evaluation suite.
- Explainability tests verify provenance completeness and deterministic
  summaries, but do not yet measure whether operators understand explanations
  better in a user study.
- MCP Resources/Prompts, Git worktree, and mock PR providers are planned but not
  implemented.
