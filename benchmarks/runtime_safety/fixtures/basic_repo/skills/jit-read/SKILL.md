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
by Capability v2.
