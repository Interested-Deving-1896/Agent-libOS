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

The default benchmark image may already expose `read_text_file`; this fixture's
primary purpose is to prove that activating a Skill and adding tool visibility
does not create a matching `filesystem:*` capability. The process still needs
`skill:read-tool` `execute` to activate the Skill and separate filesystem read
authority to use the tool successfully. This fixture is used by
`skill_tool_visibility_001`.
