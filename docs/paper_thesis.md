# Paper Thesis

Paper title:

> Agent libOS: A Runtime Substrate for Capability-Controlled Self-Evolving LLM Agents

## Thesis

Self-evolving LLM agents need a runtime substrate that lets their model-visible
action surface change without letting resource authority grow implicitly.

Modern agent systems increasingly persist memory, fork work, call shells,
activate Skills, register self-authored tools, create, execute, or commit new images,
use remote resources, ask humans, and resume across long executions. Those
behaviors are useful, but they make prompt-only control, wrapper-level tool
lists, and host isolation insufficient as the primary authority boundary.

Agent libOS argues for two coupled, Host-enforced boundaries:

```text
authority:        process identity + capability + primitive + audit
information flow: trusted data labels + Host Sink trust + exact release
```

A process may see a model-facing tool, Skill, JIT tool, image definition,
child-process handle, checkpoint, or remote endpoint. Resource access is still
decided only when a libOS primitive runs under that process id. Capability
checks, policy, human approval, provider containment, external-effect
classification, events, and audit all happen at that primitive boundary.
Trusted source labels propagate through runtime Objects, messages, Tool/JIT
results, Human answers, and provider ingress. Before runtime-mediated egress,
the Host resolves a canonical Sink identity and clearance from its durable
registry; a model cannot declare a Sink trusted. Conditional high-sensitivity
egress requires an exact one-shot Human release bound to the Sink, payload,
source versions, labels, manifest, and registry generation.

The contribution is not a larger tool catalog. The contribution is a runtime
substrate for capability-controlled self-evolution.

## Contributions

1. Runtime model.
   Agent libOS models an agent as an `AgentProcess` with process-local Object
   Memory namespaces, process-local working directories, message queues, child
   lifecycle, AgentImage registration, exec, and checkpoint commit, standard Skill activation,
   process-local Deno/TypeScript JIT tools, checkpoints, human I/O, and
   capability-controlled primitives.

2. Implementation.
   The current implementation realizes the model in Python with Capability,
   Resource Provider Substrate, runtime store persistence, audit/events, scoped
   checkpoint restore/fork/replay diagnostics, persistent LLM call accounting,
   image registry/exec/commit primitives, standard `SKILL.md` packages, JSON-RPC over
   HTTP client endpoints, MCP client tools over registered servers, and
   Deno/TypeScript JIT tools that can reach libOS only through syscall RPC.
   The same implementation carries trusted data-flow labels, enforces
   Host-owned Sink clearance at filesystem, LLM, Human, JSON-RPC, MCP, Shell,
   PTY, process, and GUI presentation boundaries, and routes provider ingress
   through the shared Protected Operation SDK.

3. Benchmark suite.
   The current implementation includes an M1 deterministic runtime-safety
   harness with 28 schema-v1 adversarial tasks, wrapper baselines, ablations,
   declared allowed/forbidden side effects, evidence-backed outcome records,
   explicit exact/prefix/glob oracle matching, and fail-closed stable metrics
   output. The suite includes a first self-evolution subset covering Skill
   activation, JIT registration, image registration/exec/checkpoint commit,
   child-process delegation, checkpoint fork, and JSON-RPC remote-resource
   visibility.

4. Evaluation.
   The paper should compare Agent libOS against direct tool wrappers,
   confirmation-prompt wrappers, and host-isolation-only baselines. Metrics
   include unauthorized side-effect rate, task success, false denial, approval
   count, wall time, token/cost accounting, overhead, audit completeness, and
   safety of self-evolution mechanisms. Unauthorized side-effect rate uses
   definitely performed effects as its denominator; false-denial rate uses only
   allowed attempts with definite performed/denied outcomes. Unknown or missing
   evidence invalidates the rate row rather than being inferred from tool
   success/failure.
   Practical workflow results must additionally label their evidence level.
   Only `native-live` rows with real ToolBroker calls, provider state oracles,
   external-effect records, and explicit operation links are runtime evidence.
   `modeled` rows are design-coverage experiments and use a separate
   denominator; trace completeness never upgrades them to native execution.

The historical repository-backed, token-free `agent_libos_full` validation was
produced from the clean source snapshot
`c03a4ec764e02bd4df59e2769edeb1278d5ea545` and artifact
`.benchmark_runs/release-c03a4ec`. It is a 28-task implementation-validation
snapshot: 28/28 task success, 28/28 safety pass, 122 normalized effects, zero
unauthorized performed effects among 97 definitely performed effects, zero
unknown outcomes/classifications, and zero false denials among 97 allowed
performed-or-denied attempts (`0/97 = 0%`). The artifact metadata and metrics
SHA-256 values are recorded in [release_status.md](release_status.md). The
mock planned-action client makes no real model request; its reported 144 LLM
tokens are deterministic usage accounting. It does not validate the current
working tree; history consolidation was not a new benchmark run and did not by
itself prove content identity. Human approval, declared LLM
provider effects, and the authorized attenuated child spawn are explicit
effects. These results support implementation validation; they are not yet the
complete comparative paper evaluation.

## Non-Goals

- Agent libOS does not claim kernel-grade sandboxing. Host isolation layers
  such as containers, Deno, WASM, or VMs are useful provider backends, not
  replacements for agent-level authority.
- Agent libOS does not solve all prompt injection. It constrains side effects
  and authority even when prompt content is adversarial.
- Agent libOS does not roll back irreversible external side effects.
  Checkpoint restore reconstructs scoped runtime state; provider-classified
  external effects are append-only and report-only unless a future provider
  compensation API implements explicit repair.
- Agent libOS does not rely on MCP, GitHub, OpenAI Agents SDK, LangGraph, or
  any external framework as a trusted security boundary. Those systems are
  workload inspiration or adapter targets; the authority boundary is inside
  the libOS primitive layer.
- Agent libOS does not treat Skills, JIT tools, Runtime Modules, image
  definitions, process exec, or JSON-RPC endpoint visibility as permission
  grants. They can change model-visible affordances; resource authority still
  comes from process capabilities, primitive checks, policy, approval, and
  audit.
- Host Sink trust constrains only runtime-mediated delivery. Trusting a Shell,
  PTY, MCP stdio executable, remote endpoint, or other provider authorizes that
  delivery; it does not control the recipient's later direct I/O or forwarding.
  Trusted provider/module code and a direct RuntimeStore administrator remain
  inside the Host TCB, and local evidence is not cryptographically tamper-proof
  against that administrator.

## Current Submission Story

The strongest systems story is: self-evolving LLM agents become safer and more
explainable when action-surface evolution is decoupled from resource authority
by a capability-controlled runtime substrate.

M0 freezes repository hygiene and the thesis. M1 establishes the initial
runtime-safety benchmark with self-evolution coverage. The runtime now also has
deterministic Explainable Operations and metadata-only Context Materialization
Manifests, Task Authority Manifests, and a first no-fallback native practical
connector lane. The next milestones should prioritize larger self-evolution
workloads, evaluating explanation usefulness, and quantitative results rather
than broad ecosystem compatibility.
