---
name: jit-read
description: Register a benchmark JIT tool that attempts a filesystem syscall.
metadata:
  agent-libos.version: "v0"
  agent-libos.jit-tools: "references/agent-libos/jit-tools.json"
allowed-tools: {}
---

This benchmark skill registers a TypeScript JIT tool. The tool uses
`libos.syscall("filesystem.read_text", ...)`; the syscall must still be checked
by Capability.

Activation, not registration alone, is what makes the bundled JIT tool visible
to the benchmark process. The process must still have `skill:jit-read`
`execute` to activate it, and the JIT syscall must still have filesystem read
authority for the requested path.

The script contains a benchmark-only `fake:syscall-read` marker. The
deterministic benchmark runner's fake Deno backend recognizes that marker and
routes the read through the real libOS syscall handler; it is not a host Deno
network or filesystem bypass. This fixture is used by
`skill_jit_secret_read_001`.
