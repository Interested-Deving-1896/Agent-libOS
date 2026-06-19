# CLI Reference

The package installs the `agent-libos` command.

The package also installs `agent-libos-gui-server`, which is used by the
Electron desktop console described in [docs/gui.md](gui.md).
Both entrypoints are implemented under `agent_libos.api`, because they are
host-facing control surfaces over the same runtime boundary.

Use `--db` to select a runtime database. The default `local` target is
in-memory. A filesystem path creates or opens a persistent SQLite database.

```bash
uv run agent-libos --db .agent_libos.sqlite <command>
```

Use `--module-manifest` and `--trusted-module` before the command name to load
trusted Runtime Modules before the runtime is used:

```bash
uv run agent-libos --db .agent_libos.sqlite \
  --module-manifest modules/example/module.yaml \
  --trusted-module example-module:v0:<source_sha256> \
  <command>
```

## Top-Level Commands

```text
init          initialize a runtime database
demo          run the deterministic local demo
audit         print audit records
llm-calls     print persisted LLM call records
processes     print process table
resources     print process resource budget, usage, and remaining budget
tools         print registered tools
spawn         spawn a process
cd            set a process working directory
exec          replace a process image and goal
exit          exit a process
llm-once      run one LLM quantum for one process
run           run the async scheduler
message       send a normal human process message
interrupt     send a human interrupt process message
checkpoint    checkpoint subcommands
skills        Skill subcommands
images        AgentImage list, inspect, and checkpoint commit subcommands
jsonrpc       JSON-RPC endpoint and call subcommands
modules       startup Runtime Module inspection and verification
human         process pending human messages manually
```

Run `uv run agent-libos <command> --help` for argparse-generated details.

## Persistent Runtime Basics

```bash
uv run agent-libos --db .agent_libos.sqlite init
uv run agent-libos --db .agent_libos.sqlite spawn --image coding-agent:v0 --goal "Summarize README.md"
uv run agent-libos --db .agent_libos.sqlite run --max-quanta 10
uv run agent-libos --db .agent_libos.sqlite processes
uv run agent-libos --db .agent_libos.sqlite resources <pid>
uv run agent-libos --db .agent_libos.sqlite audit
uv run agent-libos --db .agent_libos.sqlite tools
```

`run` uses the high-level async supervisor, so human terminal messages are
processed as part of runtime execution. Without `--max-quanta`, it runs until
the runtime becomes idle; pass `--max-quanta <n>` to bound the number of LLM/tool
quanta.

Manual queue processing remains available:

```bash
uv run agent-libos --db .agent_libos.sqlite human
```

## LLM Calls

Inspect persisted LLM action-selection calls:

```bash
uv run agent-libos --db .agent_libos.sqlite llm-calls --pid <pid>
uv run agent-libos --db .agent_libos.sqlite llm-calls --limit 20
```

Records include prompt messages, visible tools, output, tool calls, provider
ids, model/API mode, token usage when available, reasoning fields when exposed,
raw response JSON, and errors.

## Process Resources

Inspect one process's configured budget, observed usage, and remaining budget:

```bash
uv run agent-libos --db .agent_libos.sqlite resources <pid>
```

Resource accounting is process-tree scoped. Tool calls, LLM calls/tokens,
subprocess wall/CPU/RSS usage, filesystem bytes, JSON-RPC bytes, Deno syscalls,
and child-process creation are charged to the acting process and its ancestors.
Capabilities, Skill activation, image exec, checkpoint restore, and human
approval do not increase these budgets.

## Interactive Run

For a Codex CLI-style loop:

```bash
uv run agent-libos --db .agent_libos.sqlite run --interactive --pid <pid> --max-quanta 20
```

Plain text sends a normal message unless a human question or approval is
pending, in which case it answers that request.

Interactive slash commands:

- `/message <text>`: force a normal process message.
- `/interrupt <text>`: send an interrupt message.
- `/pid <pid>`: switch the default target process.
- `/exit`: leave the interactive loop.

## Process Messages

```bash
uv run agent-libos --db .agent_libos.sqlite message <pid> "Please inspect the latest result"
uv run agent-libos --db .agent_libos.sqlite interrupt <pid> "Stop current work and read this first"
uv run agent-libos --db .agent_libos.sqlite message <pid> "Use this as job input" --channel human --correlation-id job-42 --run
```

Useful options:

- `--kind normal|interrupt|...`
- `--human <name>`
- `--channel <channel>`
- `--subject <text>`
- `--correlation-id <id>`
- `--reply-to <message_id>`
- `--payload-json <object>`
- `--run`
- `--max-quanta <n>`: optional; omitted means run until idle.

## Process Builtins

```bash
uv run agent-libos --db .agent_libos.sqlite cd <pid> src
uv run agent-libos --db .agent_libos.sqlite exec images/review-agent "Review README.md" --pid <pid> --run
uv run agent-libos --db .agent_libos.sqlite exit <pid> --payload '{"done":true}'
```

For `exec`, the first positional argument is the target image. It can be an
already registered image id such as `coding-agent:v0`, or an image package
directory containing `IMAGE.yaml`. The second positional argument is the
replacement goal.

Useful exec options:

- `--replace-image`
- `--args-json <object>`
- `--preserve-memory` / `--no-preserve-memory`
- `--preserve-capabilities`
- `--run` / `--no-run`
- `--max-quanta <n>`: optional when `--run` is set; omitted means run until idle.

Exec never grants target-image `required_capabilities` automatically. If the
target is an image package, its `workspace/` seed is materialized into a private
per-process directory under `agent_outputs/image_workspaces/`.

Exit accepts either `--payload` or `--result-oid`, not both. Non-JSON payload
text is wrapped as `{"content": "<text>"}`.

## AgentImage Packages

User-defined images are directory packages:

```text
images/review-agent/
  IMAGE.yaml
  prompt.md
  tools/
    jit-tools.json
    scripts/
      summarize.ts
  resources/
  workspace/
    seed.txt
```

`IMAGE.yaml` holds structured metadata and references `prompt.md`:

```yaml
image_id: review-agent:v0
name: review-agent
prompt: prompt.md
prompt_mode: image_only
default_tools:
  - read_memory_object
  - human_output
jit_tools: tools/jit-tools.json
workspace:
  source: workspace
  working_directory: .
  grants:
    - path: .
      rights: [read, write]
      recursive: true
```

`tools/jit-tools.json` declares process-local TypeScript JIT tools whose source
files live under `tools/scripts/*.ts`. JIT tools are snapshotted as immutable
package content and are not copied into the materialized workspace.

`prompt_mode` is optional and defaults to `image_only` for custom packages.
Use `minimal_runtime` for factual runtime state sections, or `libos_default`
only when the image intentionally wants the native Agent libOS planner prompt.

`default_tools` is exact. The runtime does not add `process_exit`,
`create_memory_object`, or any other builtin automatically. List every
LLM-facing builtin the image should be able to call; package JIT tools can still
use authorized libOS syscalls internally without being mirrored as builtin
tools in the process tool table.

## Image Commands

```bash
uv run agent-libos --db .agent_libos.sqlite images list
uv run agent-libos --db .agent_libos.sqlite images inspect coding-agent:v0
uv run agent-libos --db .agent_libos.sqlite images validate images/review-agent
uv run agent-libos --db .agent_libos.sqlite images register images/review-agent
uv run agent-libos --db .agent_libos.sqlite images commit <checkpoint_id> stateful-agent:v0 --name stateful-agent
```

`images commit` creates a checkpoint-derived image artifact from the checkpoint
owner root process. It captures internal Object Memory, loaded Skills,
process-local JIT tools, tool visibility, and cwd. It does not package
filesystem/provider state. External capabilities from the checkpoint are stored
as `required_capabilities` declarations and are not granted automatically when
the committed image is spawned or execed.

Passing `--actor-pid <pid>` makes the CLI enforce that process's checkpoint
read and image write capabilities. Without it, the command runs as audited
admin CLI.

## Checkpoint Commands

```bash
uv run agent-libos --db .agent_libos.sqlite checkpoint create <pid> "before risky edit"
uv run agent-libos --db .agent_libos.sqlite checkpoint list --pid <pid>
uv run agent-libos --db .agent_libos.sqlite checkpoint inspect <checkpoint_id>
uv run agent-libos --db .agent_libos.sqlite checkpoint diff <checkpoint_id>
uv run agent-libos --db .agent_libos.sqlite checkpoint restore <checkpoint_id>
uv run agent-libos --db .agent_libos.sqlite checkpoint fork <checkpoint_id> --parent-pid <pid>
uv run agent-libos --db .agent_libos.sqlite checkpoint replay <checkpoint_id> <event_id>
```

`--actor-pid <pid>` makes the CLI enforce that process's checkpoint
capabilities. Without it, the command runs as an audited admin actor named
`cli`.

## Skill Commands

```bash
uv run agent-libos --db .agent_libos.sqlite skills discover
uv run agent-libos --db .agent_libos.sqlite skills validate skills/swe-agent
uv run agent-libos --db .agent_libos.sqlite skills inspect swe-agent
uv run agent-libos --db .agent_libos.sqlite skills register skills/swe-agent
uv run agent-libos --db .agent_libos.sqlite skills activate <pid> swe-agent
uv run agent-libos --db .agent_libos.sqlite skills unload <pid> swe-agent
```

Global Skills require exact package SHA-256 trust:

```bash
uv run agent-libos --db .agent_libos.sqlite skills trust ~/.agent-libos/skills/review-helper
uv run agent-libos --db .agent_libos.sqlite skills register ~/.agent-libos/skills/review-helper --source-type global
uv run agent-libos --db .agent_libos.sqlite skills untrust ~/.agent-libos/skills/review-helper
```

`--actor-pid <pid>` makes the CLI enforce that process's Skill, source, and
trust capabilities.

## Capability Commands

```bash
uv run agent-libos --db .agent_libos.sqlite capabilities list --subject <pid>
uv run agent-libos --db .agent_libos.sqlite capabilities inspect <capability_id>
uv run agent-libos --db .agent_libos.sqlite capabilities explain <pid> filesystem:workspace:README.md read
uv run agent-libos --db .agent_libos.sqlite capabilities grant <pid> filesystem:workspace:README.md --rights read
uv run agent-libos --db .agent_libos.sqlite capabilities delegate <parent_pid> <child_pid> filesystem:workspace:src/* --rights read
uv run agent-libos --db .agent_libos.sqlite capabilities revoke <capability_id> --reason "no longer needed"
```

Capability records are structured authority statements: typed resource
pattern, rights, `allow`/`deny`/`ask` effect, issuer lineage, delegation depth,
status, expiry, use count, constraints, and metadata. One-shot approval is
represented as `effect=allow` with `uses_remaining=1`.

Without `--actor-pid`, capability commands run as an audited admin actor. With
`--actor-pid`, the command runs as that process: `grant` requires grant/admin
authority, `delegate` requires a covering delegable parent capability, and
`revoke` requires holder, issuer, revoke, or admin authority.

## JSON-RPC Commands

```bash
uv run agent-libos --db .agent_libos.sqlite jsonrpc register endpoint.yaml
uv run agent-libos --db .agent_libos.sqlite jsonrpc list
uv run agent-libos --db .agent_libos.sqlite jsonrpc inspect demo-weather
uv run agent-libos --db .agent_libos.sqlite capabilities grant <pid> jsonrpc:demo-weather:forecast --rights read
uv run agent-libos --db .agent_libos.sqlite jsonrpc call <pid> demo-weather forecast --params-json '{"city":"Beijing"}'
uv run agent-libos --db .agent_libos.sqlite jsonrpc unregister demo-weather
```

Registry commands accept `--actor-pid <pid>` to enforce that process's
`jsonrpc_endpoint:*` or exact endpoint capabilities. Without `--actor-pid`,
they run as audited admin registry operations.

`jsonrpc call` always runs as the target process pid and requires that pid to
hold the method capability, such as
`jsonrpc:demo-weather:forecast read`. The CLI cannot supply arbitrary URLs,
headers, raw JSON-RPC method names, or request ids.

## Runtime Module Commands

Runtime Modules are trusted Python startup extensions. They are loaded with
global arguments before the selected command runs:

```bash
uv run agent-libos --db .agent_libos.sqlite modules verify modules/example/module.yaml
uv run agent-libos --db .agent_libos.sqlite --module-manifest modules/example/module.yaml --trusted-module example-module:v0:<source_sha256> modules list
uv run agent-libos --db .agent_libos.sqlite --module-manifest modules/example/module.yaml --trusted-module example-module:v0:<source_sha256> modules inspect example-module:v0
```

`modules verify` resolves the entrypoint and computes the source hash without
loading the module. `modules list` and `modules inspect` show persisted module
load records for the opened runtime database.

## Benchmark Scripts

```bash
uv run python experiments/run_benchmark.py --suite benchmarks/runtime_safety --runner agent_libos_full --limit 3 --output .benchmark_runs/m1-smoke
uv run python experiments/collect_metrics.py .benchmark_runs/m1-smoke
```

Use `--runner all` for every runner. Use repeated `--task` or
`--attack-class` to select a subset.

Real LLM mode is explicit and scoped:

```bash
uv run python experiments/run_benchmark.py --suite benchmarks/runtime_safety --runner agent_libos_full --llm real --limit 1 --output .benchmark_runs/real-smoke
```

## Example Scripts

```bash
uv run python scripts/llm_summarize_document.py README.md --auto-approve
uv run python scripts/llm_write_goal_smoke.py
uv run python scripts/run_coding_agent.py --workspace /path/to/repo --goal "Implement the requested change"
uv run python scripts/object_memory_file_copy_smoke.py
uv run python scripts/async_clock_interleave_smoke.py --iterations 3 --interval 0.2
uv run python scripts/ask_file_then_show.py --auto-answer README.md
uv run python scripts/human_llm_chat.py --mock --auto-message hello --auto-message /exit
```

On Windows PowerShell, use backslashes when convenient:

```powershell
uv run python scripts\run_coding_agent.py --workspace ..\some-repo --goal "Summarize the current project"
```
