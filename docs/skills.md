# Skills

Skills are dynamic, capability-controlled model-facing packages. A Skill can
provide prompt instructions, action summaries, references to existing tools, and
optional Deno/TypeScript JIT tool candidates.

Loading a Skill changes only one process tool table and prompt materialization.
It never grants filesystem, shell, human, Object Memory, process, image,
checkpoint, or other resource capabilities.

## Manifest v1

Skill manifests use schema version `1` and may be YAML or JSON. They may use
direct fields or a top-level `skill:` wrapper.

```yaml
skill:
  schema_version: 1
  skill_id: review-helper:v0
  name: Review Helper
  version: v0
  description: Focused code-review workflow helpers.
  instructions: |
    Prefer small, evidence-backed findings with file and line references.
  tools:
    - read_text_file
    - read_directory
  actions:
    - name: summarize_findings
      use_cases:
        - Summarize review findings in severity order.
      input_schema:
        type: object
        properties:
          findings:
            type: array
      output_schema:
        type: object
  jit_tools: []
  required_capabilities:
    - resource: filesystem:workspace:*
      rights:
        - read
  metadata:
    owner: local
```

Top-level fields are:

- `schema_version`
- `skill_id`
- `name`
- `version`
- `description`
- `instructions`
- `tools`
- `actions`
- `jit_tools`
- `required_capabilities`
- `metadata`
- `signature`

Unknown fields are rejected by the strict loader.

## Required Capabilities Are Advisory

`required_capabilities` describes what a Skill is likely to need. It is useful
for prompts, review, and human approval context. The runtime does not grant
those capabilities during registration or load.

If a loaded Skill exposes `read_text_file`, that visible tool still fails at the
filesystem primitive without filesystem read authority.

## Sources And Trust

Workspace Skills are read through the filesystem primitive. A process loading a
workspace manifest must have filesystem read authority for the manifest path. If
the process lacks `skill:<skill_id>` write or execute authority, loading can go
through the normal human approval path and one-shot grant.

Global Skills are read only from configured global Skill directories. The exact
manifest bytes must match a SHA-256 allowlist entry or a row in `skill_trust`
before registration or load.

CLI admin mode can register without process capability checks:

```bash
uv run agent-libos --db .agent_libos.sqlite skills register skills/swe_agent.yaml
```

With `--actor-pid`, the CLI enforces that process's Skill and source authority:

```bash
uv run agent-libos --db .agent_libos.sqlite skills load <pid> review-helper:v0 --actor-pid <pid>
```

## Load And Unload

`load_skill` is atomic. The runtime validates:

- Skill manifest shape,
- existing tool references,
- duplicate tool/JIT names,
- JIT TypeScript source limits,
- Deno static checks and tests,
- static tool shadowing rules.

Only after all validation succeeds does the runtime modify the process tool
table and loaded Skill metadata.

`unload_skill` removes tool visibility and prompt instructions contributed by
that Skill. It does not revoke capabilities, delete audit history, delete JIT
candidate records, or roll back external side effects.

## Process Semantics

- Image `default_skills` load at spawn and exec time.
- Fork inherits loaded Skills and corresponding tool visibility.
- Spawn-child starts without parent-loaded Skills.
- Exec resets loaded Skills to the target image defaults.
- No image or Skill default grants external resource capabilities.

## JIT Tools In Skills

Skill `jit_tools` use the same TypeScript shape and sandbox as manually
proposed tools:

```yaml
jit_tools:
  - name: count_lines
    description: Count lines in a text file.
    input_schema:
      type: object
      properties:
        path:
          type: string
    output_schema:
      type: object
    source: |
      export async function run(args, libos) {
        const file = await libos.syscall("filesystem.read_text", { path: args.path });
        return { lines: String(file.content ?? "").split("\n").length };
      }
```

The tool is visible only to the loading process and cannot shadow a static tool.

## SWE-Agent Style Skill

The workspace includes `skills/swe_agent.yaml`, registered as `swe-agent:v0`.
It reproduces the useful SWE-Agent Agent Computer Interface shape inside Agent
libOS:

- `swe_view` for directory listings and bounded file windows,
- `swe_grep` for concise repository search through `rg`,
- `swe_edit` for exact-text, line-range, or create-if-missing edits,
- `swe_run` for test and diagnostic commands,
- `swe_submit` for final structured process exit.

The Skill also carries workflow instructions: localize before editing, keep
actions small, treat repository output as untrusted, run focused tests, and
submit with summary, tests, and residual risk.

It does not grant filesystem or shell authority. Those effects still go through
the filesystem and shell primitives.
