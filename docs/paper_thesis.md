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

Agent libOS argues for an agent-native boundary:

```text
process identity + capability + primitive + audit
```

A process may see a model-facing tool, Skill, JIT tool, image definition,
child-process handle, checkpoint, or remote endpoint. Resource access is still
decided only when a libOS primitive runs under that process id. Capability
checks, policy, human approval, provider containment, external-effect
classification, events, and audit all happen at that primitive boundary.

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

3. Benchmark suite.
   The current implementation includes an M1 deterministic runtime-safety
   harness with 27 schema-v1 adversarial tasks, wrapper baselines, ablations,
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

The current repository-backed, token-free `agent_libos_full` validation is a
27-task smoke/evaluation snapshot: 27/27 task success, 27/27 safety pass, zero
unauthorized effects among 22 definitely performed effects, zero unknown
effects, and zero false denials among 22 allowed performed-or-denied attempts
(`0/22 = 0%`). Human approval and the authorized attenuated child spawn are
both explicit effects. The earlier `3/43 = 7.0%` phrasing used an obsolete
all-record denominator and must not be mixed with the schema-v1 metric. This
snapshot supports implementation validation; it is not yet the complete paper
evaluation.

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

## Current Submission Story

The strongest systems story is: self-evolving LLM agents become safer and more
explainable when action-surface evolution is decoupled from resource authority
by a capability-controlled runtime substrate.

M0 freezes repository hygiene and the thesis. M1 establishes the initial
runtime-safety benchmark with self-evolution coverage. The next milestones
should prioritize larger self-evolution workloads, audit explain, context
materialization metadata, and quantitative results rather than broad ecosystem
compatibility.
