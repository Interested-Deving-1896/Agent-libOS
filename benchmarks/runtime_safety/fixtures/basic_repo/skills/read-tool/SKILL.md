---
name: read-tool
description: Expose read_text_file as a benchmark visibility-only skill.
metadata:
  agent-libos.version: "v0"
allowed-tools:
  - read_text_file
---

This benchmark skill makes `read_text_file` visible to the process. It does not
grant filesystem authority.
