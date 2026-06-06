# Capabilities And Permission Policy

Agent libOS uses explicit capabilities for resource authority. A visible tool,
loaded Skill, object name, path string, image id, or checkpoint id is not enough
to perform a protected operation.

## Rights

Capabilities are checked against a resource string and one or more rights. The
common rights are:

- `read`: inspect or materialize a resource.
- `write`: create or modify a resource.
- `delete`: remove a resource.
- `execute`: run or load a resource.
- `list`: enumerate namespace-like resources.
- `materialize`: include object content in a prompt or tool result.
- `link`: create Object Memory links.
- `diff`: compare object or checkpoint state.
- `admin`: perform destructive or policy-changing operations.

The exact right set depends on the primitive.

## Resource Names

Important resource naming conventions include:

- `filesystem:workspace:<path>` for workspace files and directories.
- `filesystem:workspace:<dir>/*` for directory subtree grants.
- `shell:*` for process-scoped shell policy grants.
- `human:<name>` for human output, questions, and approvals.
- `object:<oid>` for Object Memory content.
- `namespace:<namespace>` for Object Memory namespace listing and name
  resolution.
- `image:<image_id>` and `image:*` for image registration.
- `skill:<skill_id>` and `skill:*` for Skill operations.
- `skill_source:workspace:<relative_path>` and
  `skill_source:global:<source_id>` for Skill source authority.
- `skill_trust:*` or `skill_trust:<sha256>` for global Skill trust.
- `checkpoint:process:<pid>`, `checkpoint:<checkpoint_id>`, and
  `checkpoint:*` for checkpoint operations.

Resource names are matched by the capability manager, not by model text. Prefer
the narrowest resource that covers the intended operation.

## Tool Visibility Is Not Authority

The process tool table controls which LLM-facing tools a process may call.
Capabilities control whether primitives can perform protected effects.

For example, a process can see `write_text_file` and still fail to write
`src/app.py` if it lacks write authority for
`filesystem:workspace:src/app.py` or a covering subtree grant.

The same rule applies to Skills and JIT tools. Loading a Skill can add
`run_shell_command` or `swe_run` to one process table, but shell execution still
requires shell authority and policy approval at the shell primitive.

## One-Shot Grants

Permission policy can create one-shot capabilities. A one-shot grant is valid
for one successful primitive use and is then consumed. Failed precondition
checks do not silently convert into broad persistent authority.

One-shot grants are used for per-use human approvals. They make approval
decisions explicit in audit without permanently widening the process.

## Human Approval

`request_permission` asks the human to choose a policy for a resource/right
pair. Supported decisions include:

- `always_allow`: grant reusable authority.
- `always_deny`: deny future attempts.
- `ask_each_time`: prompt for approval at each primitive use.

When `ask_each_time` applies, the primitive creates a human approval request and
waits inside the operation. The caller eventually receives either the final
payload or a final denial error.

Approval context includes path, resource, overwrite risk, byte count, SHA-256,
target state, and an escaped content preview when available.

## Filesystem Authority

Filesystem capabilities can target:

- exact files, such as `filesystem:workspace:README.md`,
- directory subtrees, such as `filesystem:workspace:agent_outputs/*`,
- the whole workspace, such as `filesystem:workspace:*`.

Relative paths resolve from the caller process working directory. The
filesystem primitive enforces workspace containment before host provider calls.
Path strings that escape the workspace are rejected even if a model can produce
them.

Read, write, and delete are separate rights. Granting read over a directory does
not grant write or delete.

## Shell Policy

Shell execution is argv-only. The model-facing tool and syscall accept token
arrays, not shell command strings. Pipes, redirects, wildcard expansion, and
command chaining must be requested explicitly through an interpreter executable
such as `bash`, `sh`, `cmd`, or `powershell`, where policy matching can inspect
the interpreter token.

The built-in policy levels are:

- `always_deny`: reject every shell command.
- `allowlist_auto_else_ask`: allow configured safe argv rules, ask otherwise.
- `blocklist_ask_else_auto`: ask for configured risky argv rules, allow
  others.
- `always_allow`: allow all commands. This is intentionally high risk.

Allow and block lists match tokenized argv rules, not arbitrary substrings.
Bare executable names do not match path-qualified executables by accident.
Block-list checks also scan nested executable-looking argv tokens such as
`bash`, `powershell`, `python`, or `curl`.

## Child Authority

Fork and spawn do not implicitly inherit broad external authority. A child can
inherit only explicit subsets that the parent already holds. Tests cover the
important non-escalation cases:

- fork does not inherit parent filesystem write authority by default,
- spawn creates a fresh child without parent memory or default caps,
- a child cannot inherit broader authority than the parent has,
- exec does not grant target-image required capabilities.
