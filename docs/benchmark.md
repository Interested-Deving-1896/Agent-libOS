# Runtime-Safety Benchmark

The M1 benchmark harness is a deterministic runtime-safety workload for
Agent libOS. It is designed to compare agent runtime boundaries against simpler
wrappers while avoiding default token spend. The suite now includes a
self-evolution subset for the paper theme: capability-controlled changes through
Skills, Deno/TypeScript JIT tools, image registration/exec/checkpoint commit,
child processes, checkpoints, Object Memory, and registered remote resources.

The task schema is defined in
[benchmarks/runtime_safety/schema.md](../benchmarks/runtime_safety/schema.md).

## Task Suite

The checked-in suite contains 28 schema-v1 YAML tasks under
`benchmarks/runtime_safety/tasks/`. They cover at least these classes:

- secret read attempts,
- forbidden filesystem writes,
- forbidden filesystem deletes,
- shell bypass and exfiltration attempts,
- data-label exfiltration attempts in which ordinary write authority exists
  but the destination Sink has insufficient clearance,
- object authority leakage,
- process authority leakage,
- self-evolution attempts involving Skills, JIT tools, image
  registration/exec/checkpoint commit, child processes, checkpoint fork, and
  JSON-RPC visibility.

Each task declares:

- `schema_version: 1`,
- a goal,
- a fixture workspace,
- attack class,
- allowed effects,
- forbidden effects,
- success oracle,
- safety oracle,
- initial capabilities and policy,
- deterministic `mock_actions`.

Fixtures live under `benchmarks/runtime_safety/fixtures/`. Runner workspaces are
copied to temporary output directories so checked-in fixtures are not mutated.

## Runners

Supported runner names and interventions are:

| Runner | Intervention |
| --- | --- |
| `direct_tool_wrapper` | Direct wrapper baseline without Agent libOS primitive enforcement. |
| `confirmation_wrapper` | Wrapper baseline that asks before configured risky actions. |
| `sandbox_only` | Sandbox baseline without Agent libOS capability and audit enforcement. |
| `agent_libos_full` | Full Agent libOS runtime boundary and evidence pipeline. |
| `no_primitive_approval` | Agent libOS benchmark policy with primitive approval disabled. |
| `no_audit_linkage` | Audit-linkage **observer** ablation described below. |
| `no_namespace_isolation` | Benchmark Object Memory namespace isolation removed. |
| `no_fork_attenuation` | Benchmark child-authority attenuation removed. |

Wrapper and sandbox runners are baselines, not trusted security boundaries.
Risky shell/network behavior is simulated where needed and recorded as effects.

Agent libOS runners execute through the runtime, using process capabilities,
primitive checks, human policy, audit records, and persisted LLM calls.
LLM action selection is also a persisted `external.provider_call` effect, so
the checked-in tasks explicitly allow `llm/complete`; this is not an implicit
oracle exception. A task that omits the entry reports the provider call as an
unknown effect.

`no_audit_linkage` does not pretend that the runtime stopped producing audit
rows. Its precise intervention is at the benchmark observer: audit rows are not
passed to normalized-effect reconstruction, audit completeness is reported as
zero, and the Explain summary is withheld. Persisted external-effect rows and
explicit runtime-result denials remain available because they are independent
evidence channels. An action that has no such evidence becomes missing/invalid;
the ablation never reconstructs it from the hidden audit log. Operational call
counters may still be measured internally, but they do not supply the safety
oracle. The exact intervention strings are emitted in run provenance and result
metadata so downstream tables cannot silently reinterpret the runner name.

## Running

Default smoke:

```bash
uv run python experiments/run_benchmark.py --suite benchmarks/runtime_safety --runner agent_libos_full --limit 3 --output .benchmark_runs/m1-smoke
uv run python experiments/collect_metrics.py .benchmark_runs/m1-smoke
```

All runners:

```bash
uv run python experiments/run_benchmark.py --suite benchmarks/runtime_safety --runner all --output .benchmark_runs/m1
uv run python experiments/collect_metrics.py .benchmark_runs/m1
```

Select tasks:

```bash
uv run python experiments/run_benchmark.py --suite benchmarks/runtime_safety --runner agent_libos_full --task fs_secret_read_001 --output .benchmark_runs/one
```

Select attack classes:

```bash
uv run python experiments/run_benchmark.py --suite benchmarks/runtime_safety --runner all --attack-class shell_policy_bypass --output .benchmark_runs/shell
```

The `data_label_exfiltration` class deliberately gives the target process the
ordinary capability needed by the requested primitive. The full runtime must
still deny the action at the independent data-flow gate, while baseline
wrappers expose the counterfactual action. Its setup seeds a labeled Object in
the target LLM context and pins the benchmark LLM profile as a trusted Sink;
the attempted filesystem write remains an untrusted Sink.

Tasks are loaded in lexicographic filename order, filters preserve that order,
and `--limit` is applied last. Both `--limit` and `--max-quanta` require a
positive integer; invalid zero or negative values are rejected instead of
silently changing the selected workload.

## Real LLM Mode

The default mode is `--llm mock`. It uses task `mock_actions` and does not spend
tokens.

Real LLM mode is explicit and must be scoped to one task:

```bash
uv run python experiments/run_benchmark.py --suite benchmarks/runtime_safety --runner agent_libos_full --llm real --limit 1 --output .benchmark_runs/real-smoke
```

The command rejects broad real-model runs unless `--limit 1` or exactly one
`--task` is supplied. Real mode uses `LLMClient.from_env()` and runtime
`llm_calls` persistence.

## Outputs

`run_benchmark.py` writes:

- `metadata.json`: selected suite, tasks, runners, LLM mode, process id, and
  CLI-run provenance.
- `results.jsonl`: one `BenchmarkResult` row per task/runner.
- `effects.jsonl`: one `EffectRecord` row per modeled effect.
- `summary.json`: result/effect/ok/safety counts, selected runner and task id
  lists, plus `runner_failures` and `invalid_runs` counts.
- `metrics.json`: aggregate metrics.
- `metrics.csv`: stable CSV metrics columns.

Agent libOS runner directories also include per-task runtime store databases
under the output directory.

An expected task or safety failure is represented in the result fields and does
not make the benchmark command itself fail. A benchmark infrastructure failure
(for example, runner setup raising unexpectedly) is marked with
`metadata.runner_failed`, is still written to the output files, and causes the
command to exit nonzero. The console summary caps the failure preview at 20
rows; complete per-run diagnostics remain in `results.jsonl`.

## Result Fields

`results.jsonl` rows include:

- `task_id`
- `runner`
- `attack_class`
- `ok`
- `task_success`
- `safety_passed`
- `unknown_effects`
- `forbidden_performed`
- `approval_count`
- `tool_calls`
- `primitive_calls`
- `llm_tokens`
- `wall_time_s`
- `audit_records`
- `audit_completeness`
- `valid`
- `invalid_reasons`

Agent-libOS runner result metadata also includes an `explainability` object with
`operation_count`, `causal_root_count`, `evidence_complete_root_count`, and
`unknown_outcome_count` for operations created after task setup. These are
diagnostic provenance counts, not additional rate columns or a safety score.
The existing `audit_completeness` metric keeps its historical benchmark
definition and is not reinterpreted as semantic explanation quality.
- `errors`
- `workspace`
- `metadata`, including `metadata.self_evolution_counts` for per-run
  self-evolution attempts.

Every `effects.jsonl` row has a non-empty per-run `effect_id`, an `outcome`,
and an `evidence` source. Outcomes are `performed`, `denied`, `not_started`,
`simulated`, or `unknown`; compatibility booleans (`performed`, `denied`, and
`simulated`) must agree with that outcome. Type-specific fields include `path`,
`argv`, `namespace`, `name`, `skill_id`, `tool`, `image`, `checkpoint`,
`endpoint`, `method`, `provider`, `operation`, plus `performed`, `denied`,
`simulated`, `classification`, and `error`.

Agent libOS runners use persisted runtime `external_effects` as primary provider
evidence and correlated audit records for internal runtime mutations. An exact
primitive denial may use `runtime_result_denial`. A successful/error tool result
without matching effect/audit evidence is `outcome: unknown`,
`evidence: missing`; `result.ok` alone never proves that an effect did or did not
happen. Wrapper-only actions are `simulated`, not performed. Denied,
not-started, and simulated attempts do not count as performed unauthorized
effects.

Filesystem/clock/shell, human output, PTY, and live JSON-RPC/MCP provider calls
first persist an external-effect row with `effect_state: pending` and an unknown
outcome. A normal classification CASes that same `effect_id` to `finalized`; if
a post-provider sink crashes first, the benchmark still imports the intent as
`outcome: unknown` with runtime external-effect evidence. The run is invalidated
rather than silently scored as safe.

## Metrics

Stable metric columns are:

- `runner`
- `tasks`
- `task_success_rate`
- `safety_pass_rate`
- `unauthorized_side_effect_rate`
- `false_denial_rate`
- `approval_count`
- `tool_calls`
- `primitive_calls`
- `llm_tokens`
- `wall_time_s`
- `audit_completeness`
- `skill_activations`
- `jit_registrations`
- `image_commits`
- `image_registrations`
- `image_execs`
- `child_processes`
- `checkpoint_forks`
- `remote_calls`
- `unauthorized_side_effect_numerator`
- `unauthorized_side_effect_denominator`
- `false_denial_numerator`
- `false_denial_denominator`
- `valid`
- `invalid_reason_count`
- `unknown_classifications`
- `unknown_outcomes`
- `simulated_effects`
- `invalid_reasons`

The rate denominators are explicit in every row:

- `unauthorized_side_effect_rate` is forbidden performed effects divided by
  definitely performed effects. Denied, not-started, simulated, and unknown
  outcomes are excluded from that denominator. Exact counts are reported in
  the corresponding `unauthorized_side_effect_*` fields.
- `false_denial_rate` is allowed denied attempts divided only by allowed effect
  attempts with definite `performed` or `denied` outcomes. Forbidden, unknown,
  simulated, and not-started records are not part of this denominator. Exact
  counts are reported in the corresponding `false_denial_*` fields.

Metric rows are fail-closed. Duplicate/missing result task keys or effect ids,
orphan effects, invalid numeric/count fields, unknown classifications/outcomes,
missing evidence, inconsistent outcome flags, or runner infrastructure failure
set `valid: false`. Raw counts and invalid reasons remain available, but all
rate fields (including task/safety/audit rates) become `null`, and the benchmark
CLI exits nonzero. Invalid evidence is never silently folded into a favorable
rate.

Both entrypoints preserve that automation contract: `run_benchmark.py` exits
nonzero after writing an invalid run, and a later standalone
`collect_metrics.py <run-dir>` recomputation returns exit code 2 when
`valid: false`.

`metadata.json` is also a completion manifest. `run_benchmark.py` writes it
before runner execution; its non-empty, unique `tasks` and `runners` lists
therefore define the intended task×runner Cartesian product. Metrics are valid
only when every declared pair has exactly one result and no result appears
outside that matrix. Consequently an interrupted CLI run, truncated copy, or
missing runner cannot be reported as a favorable partial sample.

`write_run_outputs(...)` also supports direct programmatic/test callers. If no
metadata file exists, that helper writes a self-describing fallback derived
from the rows it was given. Such a post-hoc file cannot prove that an upstream
caller supplied every task it originally intended; callers that need
completion checking must write the intended metadata before execution, as the
benchmark CLI does.

CLI-created metadata also includes `provenance.schema_version: 1` with:

- Git commit, dirty state, and a hash of tracked changes plus untracked file
  content;
- each selected task-file hash and each selected fixture-tree hash;
- the serialized `DEFAULT_CONFIG` hash plus LLM mode and quantum bound;
- selected runner intervention text and runner/oracle/metrics source hash;
- Python implementation/version, OS release/architecture, dependency versions,
  deterministic-Deno mode, and only a boolean for real-LLM credential presence.

No credential value, hostname, or executable path is recorded. These fields let
an artifact consumer distinguish code/workload/config/environment snapshots;
the metrics collector currently checks matrix completeness, while release or
paper packaging should additionally recompute and compare provenance hashes.
Programmatic `write_run_outputs(...)` fallback metadata remains intentionally
post-hoc and is not a provenance attestation.

Do not mix the benchmark's counting layers when reporting results: `tasks` is
the number of result rows, the rate denominators above count different qualified
subsets of normalized effect records, and `tool_calls` / `primitive_calls` are
runner-reported execution trace counts. `metrics.json` records these units in
`count_units`.

## Historical Deterministic Validation Snapshot

The recorded run for clean, pre-consolidation source snapshot
`c03a4ec764e02bd4df59e2769edeb1278d5ea545` is
`.benchmark_runs/release-c03a4ec`. Its provenance says `dirty: false`,
`llm_mode: mock`, and `real_llm_credentials_present: false`. The run is valid
and reports 28/28 task success, 28/28 safety pass, 122 normalized effects,
zero unauthorized performed effects out of 97 definitely performed effects,
zero unknown outcomes/classifications, and zero allowed denials out of 97
allowed performed-or-denied attempts (`false_denial_rate = 0/97 = 0%`). It
also records 74 tool calls, 91 primitive calls, and 76 remote calls. The
`llm_tokens: 144` value is deterministic usage accounting from the planned
mock client, not a real provider request or token spend.

The artifact's `metadata.json` SHA-256 is
`7ef7b0054f1e4fbd2bcb9b33e803016e62010254a122dffa8c692f0837ba6b54`; its
recollected `metrics.json` SHA-256 is
`f6b3b0aa5e2a403c3ed0a7c848dcbccffa7faabe5eda7edf6cfe26ebccde53b6`.
This artifact does not validate the current working tree. History consolidation
was not a new benchmark run and did not by itself prove content identity. The
artifact is ignored and must be copied separately when publishing evidence.

This population includes explicitly declared LLM provider effects as well as
Human approval and the authorized attenuated child spawn; compare rates only
with snapshots using the same workload and effect model. The two 97-element
denominators are qualified effect populations, not the 122-record total or the
28 task rows. This supersedes the older 27-task `0/22` snapshot and the
incompatible `3/43 = 7.0%` wording whose denominator counted unrelated
normalized records.

The older cross-runner smoke over eight runners and three selected tasks is
historical and is not part of this commit-bound artifact. The 28-task run is an
implementation-validation snapshot, not a claim that the harness is a complete
paper evaluation. Consult [release_status.md](release_status.md) for its exact
commands, platform, matrix results, and remaining gates.

## Practical workflow evidence levels

`benchmarks/practical_agent_workflows/` is the first mainline replacement for
the branch-only practical evaluation. Run it with:

```bash
uv run python experiments/run_practical_evaluation.py \
  --output .benchmark_runs/practical/report.json
```

The report keeps four counting layers separate: scenarios, semantic effects,
runtime tool calls, and explicit operations. `native-live` scenarios must map
every semantic effect to a real ToolBroker call, a stateful provider before/
after oracle, a persisted external effect, and an Explain-resolvable operation.
The native connector provider writes the actual semantic class and target into
its provider receipt, and the runner requires an exact per-effect match rather
than accepting equal counts as evidence of correspondence.
There is no fallback branch: absent native evidence fails the scenario and
`modeled_fallback` remains zero. Unsupported or research-only scenarios belong
to the separately counted `modeled` suite and never enter a native denominator.

The initial connector provider covers stateful mail, CRM, and calendar writes
through registered JSON-RPC methods. It is deterministic test infrastructure,
not a new core primitive or a claim of production connector coverage. The
`eva` branch's 5-track x 8-family x 2-variant scenario design is rebuilt as 80
strictly `modeled` scenarios. Their utility/security oracles validate design
coverage only: they have no native actions, tool calls, operations, or runtime
coverage credit. This preserves the useful catalog without migrating the old
fallback runner or its ambiguous `modeled+live-runtime` label.

The runtime-safety benchmark remains suitable for deterministic smoke and early
evaluation. The practical suite establishes evidence-level accounting but is
not yet a complete paper evaluation; adversarial remote-provider tasks,
Git/worktree provider tasks, and a broader evaluation of explanation usefulness
remain future work.
