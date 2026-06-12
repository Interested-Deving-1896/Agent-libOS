# Skills

Agent libOS Skills use the standard Agent Skills package shape: a directory
with a required `SKILL.md` file, optional `scripts/`, `references/`, and
`assets/` resources, and progressive disclosure from catalog metadata to full
instructions and bundled resources.

Skills are not a permission mechanism. Activating a Skill changes only one
process's prompt context and tool visibility. They are part of the
self-evolving action surface, not the authority subsystem. Filesystem, shell,
Object Memory, JSON-RPC, process, checkpoint, and human effects still go
through primitives and Capability v2.

## Package Shape

```text
skills/review-helper/
  SKILL.md
  scripts/
    count_lines.ts
  references/
    agent-libos/
      actions.json
      required-capabilities.json
      jit-tools.json
    workflow.md
  assets/
```

`SKILL.md` must start with YAML frontmatter:

```yaml
---
name: review-helper
description: Focused code-review workflow helpers.
license: Apache-2.0
compatibility: agent-libos>=0.1.0
allowed-tools:
  - read_text_file
  - read_directory
metadata:
  agent-libos.version: v0
  agent-libos.actions: references/agent-libos/actions.json
  agent-libos.required-capabilities: references/agent-libos/required-capabilities.json
  agent-libos.jit-tools: references/agent-libos/jit-tools.json
---

# Review Helper

Prefer small, evidence-backed findings with file and line references.
```

Supported frontmatter fields are `name`, `description`, `license`,
`compatibility`, `allowed-tools`, and `metadata`. The `name` must be lowercase
letters, digits, and hyphens, and must match the package directory name.
`metadata` values must be strings.

Agent libOS reserves the `agent-libos.*` metadata namespace. Complex extension
data lives in `references/agent-libos/*.json`; metadata only points at those
relative files.

## Progressive Disclosure

Discovery returns catalog fields such as `name`, `description`, source, package
hash, and high-level tool/action names. Activation materializes the full
`SKILL.md` body into the process prompt. Bundled resources are read explicitly
with `read_skill_resource` and only from the registered package snapshot.

This prevents a Skill from keeping ambient read authority to the workspace path
where it was registered.

## LibOS Extensions

`allowed-tools` adds existing static tools to the process tool table during
activation. The tools remain wrappers over primitives; visibility does not imply
resource authority.

`references/agent-libos/jit-tools.json` declares TypeScript JIT tools. Each
entry references a `scripts/*.ts` source file:

```json
[
  {
    "name": "count_lines",
    "description": "Count lines in a text file.",
    "source_path": "scripts/count_lines.ts",
    "input_schema": {"type": "object"},
    "output_schema": {"type": "object"},
    "tests": []
  }
]
```

JIT sources are snapshotted at registration, validated through the Deno
sandbox, and can only access libOS through `libos.syscall()`.

`actions.json` and `required-capabilities.json` are advisory prompt metadata.
They do not create capabilities.

## Sources And Trust

Workspace Skills are registered through the filesystem primitive when an
AgentProcess is the actor. The process must be able to read `SKILL.md` and any
referenced metadata or script resources. Registration also snapshots additional
bundled files under `scripts/`, `references/`, and `assets/` when the process
already has the corresponding directory and file read authority; it does not
grant or prompt for ambient package-directory reads just to discover optional
resources.

Global Skills are read only from configured global Skill directories. Their
full package SHA-256 must be trusted before registration:

```bash
uv run agent-libos --db .agent_libos.sqlite skills trust ~/.agent-libos/skills/review-helper
uv run agent-libos --db .agent_libos.sqlite skills register ~/.agent-libos/skills/review-helper --source-type global
```

Admin CLI registration can read and snapshot a workspace package directly:

```bash
uv run agent-libos --db .agent_libos.sqlite skills validate skills/swe-agent
uv run agent-libos --db .agent_libos.sqlite skills register skills/swe-agent
uv run agent-libos --db .agent_libos.sqlite skills activate <pid> swe-agent
```

With `--actor-pid`, the CLI enforces that process's filesystem and Skill
capabilities.

## Activation And Unload

`activate_skill` is atomic. The runtime validates the package, existing tool
references, duplicate tool/JIT names, TypeScript source limits, Deno static
checks and tests, and static tool shadowing before it modifies the process tool
table or loaded Skill metadata.

`unload_skill` removes tool visibility and prompt instructions contributed by
that Skill. It does not revoke capabilities, delete audit history, delete JIT
candidate records, or roll back external side effects.

## Process Semantics

- Image `default_skills` activate at spawn and exec time.
- Fork inherits activated Skills and corresponding tool visibility.
- Spawn-child starts without parent-activated Skills.
- Exec resets activated Skills to the target image defaults.
- No image or Skill default grants external resource capabilities.

## SWE-Agent Style Skill

The workspace includes `skills/swe-agent`, registered as `swe-agent`. It
reproduces the useful SWE-Agent Agent Computer Interface shape inside Agent
libOS:

- `swe_view` for directory listings and bounded file windows,
- `swe_grep` for concise repository search through `rg`,
- `swe_edit` for exact-text, line-range, or create-if-missing edits,
- `swe_run` for test and diagnostic commands,
- `swe_submit` for final structured process exit.

The Skill carries workflow instructions for localizing before editing, keeping
actions small, treating repository output as untrusted, running focused tests,
and submitting with summary, tests, and residual risk. It does not grant
filesystem or shell authority.
