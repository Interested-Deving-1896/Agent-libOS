# Paper Thesis

Temporary anonymous system name: `Primitive Agent Runtime` (`PAR`).

## Thesis

Long-running LLM agents need an agent-native runtime authority boundary. Current
agent stacks often expose a model-visible tool list and treat wrapper code,
prompts, or container placement as the main control point. That is too weak for
agents that fork work, write code, call shells, persist memory, ask humans, and
delegate to self-authored tools over many steps.

PAR argues that an agent runtime should separate tool visibility from resource
authority. A process may see a model-facing tool, but resource access is decided
only at primitive use by process identity, capabilities, policy, human approval,
and audit. The core contribution is not a larger tool catalog. The contribution
is the runtime boundary:

```text
process identity + capability + primitive + audit
```

This boundary makes agent actions schedulable, interruptible, explainable, and
measurably safer under adversarial workloads.

## Contributions

1. Runtime model. PAR models an agent as an `AgentProcess` with process-local
   Object Memory namespaces, process-local working directories, message queues,
   child process lifecycle, and capability-controlled primitives. Human I/O is a
   device-like runtime primitive rather than prompt text.

2. Implementation. The current prototype implements the model in Python with a
   Resource Provider Substrate, SQLite persistence, audit records, persistent LLM
   call accounting, shell/image/filesystem/process/human/memory primitives, and
   Deno/TypeScript JIT tools that can reach libOS only through syscall RPC.

3. Benchmark suite. The planned evaluation uses adversarial coding-agent and
   runtime-safety tasks with declared allowed and forbidden side effects. The
   benchmark is designed to test whether the runtime blocks unauthorized effects
   while preserving task success and keeping approval burden measurable.

4. Evaluation. The paper should compare PAR against direct tool wrappers,
   confirmation-prompt wrappers, and host-isolation-only baselines. Metrics
   include unauthorized side-effect rate, task success, false denial, approval
   count, wall time, token/cost accounting, overhead, and audit completeness.

## Non-Goals

- PAR does not claim kernel-grade sandboxing. Host isolation layers such as
  containers, Deno, WASM, or VMs are useful provider backends, not replacements
  for agent-level authority.
- PAR does not solve all prompt injection. It constrains side effects and
  authority even when prompt content is adversarial.
- PAR does not roll back irreversible external side effects. The runtime can
  audit, explain, deny, or compensate when modeled, but external reality is not
  rewound by checkpoint restore.
- PAR does not rely on MCP, GitHub, OpenAI Agents SDK, or LangGraph as a
  trusted security boundary. Those systems are workload inspiration or possible
  adapters; PAR's boundary is inside the libOS primitive layer.

## Current Submission Story

The strongest submission story is a systems story: primitive-level authority
boundaries make long-running LLM agents safer and more explainable at acceptable
cost. M0 freezes the story and repository hygiene. M1 through M4 should focus on
benchmark tasks, baselines, audit explain, context materialization, and
quantitative results rather than broad ecosystem compatibility.
