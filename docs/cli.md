# CLI Reference

The package installs the `agent-libos` command.

Use `--db` to select a runtime database. The default `local` target is
in-memory. A filesystem path creates or opens a persistent SQLite database.

```bash
uv run agent-libos --db .agent_libos.sqlite <command>
```

## Top-Level Commands

```text
init          initialize a runtime database
demo          run the deterministic local demo
audit         print audit records
llm-calls     print persisted LLM call records
processes     print process table
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
human         process pending human messages manually
grant-tool    deprecated; image tool tables are fixed at creation
```

Run `uv run agent-libos <command> --help` for argparse-generated details.

## Persistent Runtime Basics

```bash
uv run agent-libos --db .agent_libos.sqlite init
uv run agent-libos --db .agent_libos.sqlite spawn --image coding-agent:v0 --goal "Summarize README.md"
uv run agent-libos --db .agent_libos.sqlite run --max-quanta 10
uv run agent-libos --db .agent_libos.sqlite processes
uv run agent-libos --db .agent_libos.sqlite audit
uv run agent-libos --db .agent_libos.sqlite tools
```

`run` uses the high-level async supervisor, so human terminal messages are
processed as part of runtime execution.

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
- `--max-quanta <n>`

## Process Builtins

```bash
uv run agent-libos --db .agent_libos.sqlite cd <pid> src
uv run agent-libos --db .agent_libos.sqlite exec image.yaml "Review README.md" --pid <pid> --run
uv run agent-libos --db .agent_libos.sqlite exit <pid> --payload '{"done":true}'
```

For `exec`, the first positional argument is the target image. It can be an
already registered image id such as `coding-agent:v0`, or a `.yaml` / `.yml`
AgentImage manifest path. The second positional argument is the replacement
goal.

Useful exec options:

- `--replace-image`
- `--args-json <object>`
- `--preserve-memory` / `--no-preserve-memory`
- `--preserve-capabilities`
- `--run` / `--no-run`
- `--max-quanta <n>`

Exec never grants target-image `required_capabilities` automatically.

Exit accepts either `--payload` or `--result-oid`, not both. Non-JSON payload
text is wrapped as `{"content": "<text>"}`.

## AgentImage YAML

Image manifests accepted by CLI exec or `load_image_from_yaml` can use a
top-level `image:` mapping or direct image fields:

```yaml
image:
  image_id: yaml-agent:v0
  name: yaml-agent
  system_prompt: |
    Use the smallest safe tool sequence.
  default_tools:
    - read_memory_object
    - human_output
  default_skills: []
  context_policy: evidence_first
  safety_profile: review
  metadata:
    role: example
```

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
uv run agent-libos --db .agent_libos.sqlite skills inspect swe-agent:v0
uv run agent-libos --db .agent_libos.sqlite skills register skills/swe_agent.yaml
uv run agent-libos --db .agent_libos.sqlite skills load <pid> swe-agent:v0
uv run agent-libos --db .agent_libos.sqlite skills unload <pid> swe-agent:v0
```

Global Skills require exact-byte trust:

```bash
uv run agent-libos --db .agent_libos.sqlite skills trust ~/.agent-libos/skills/review.yaml
uv run agent-libos --db .agent_libos.sqlite skills register ~/.agent-libos/skills/review.yaml --source-type global
uv run agent-libos --db .agent_libos.sqlite skills untrust ~/.agent-libos/skills/review.yaml
```

`--actor-pid <pid>` makes the CLI enforce that process's Skill, source, and
trust capabilities.

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
