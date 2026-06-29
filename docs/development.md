# Development Guide

This guide covers local setup, regression checks, real Deno behavior, optional
real LLM paths, and documentation rules for Agent libOS contributors.

## Setup

Install dependencies:

```bash
uv sync --all-groups
npm --prefix gui install
```

Use frozen dependency resolution for artifact and CI-style checks:

```bash
uv sync --frozen --all-groups
npm --prefix gui install
```

Deno-backed tests run by default when `deno` is installed. If `deno` is absent,
tests marked `real_deno` skip with a clear pytest reason; use
`--skip-real-deno` only when a run intentionally excludes them. To validate and
run real Deno/TypeScript JIT tools from another binary, pass a runtime config
built with `dataclasses.replace(DEFAULT_CONFIG, tools=replace(...))`.

## Standard Checks

Run:

```bash
uv run python -m compileall agent_libos tests scripts experiments benchmarks
uv run python scripts/test_matrix.py --lane unit
uv run python scripts/test_matrix.py --lane security
uv run python scripts/test_matrix.py --lane runtime
uv run python scripts/check_test_invariants.py
uv run python scripts/test_matrix.py --lane gui
git diff --check
```

Run all deterministic Python lanes:

```bash
uv run python scripts/test_matrix.py --lane all
```

Use pytest-xdist workers for faster local Python feedback:

```bash
uv run python scripts/test_matrix.py --lane all --workers 4
uv run python scripts/test_matrix.py --lane runtime --workers auto
```

`--workers` applies only to Python lanes. The `runtime` and `all` lanes default
to bounded parallel execution with at most four workers and `--dist worksteal`,
which keeps CI runtime below the lane budget while balancing long persistence and
runtime-reopen tests. Pass `--workers 1` for serial failure diagnosis, or set
`AGENT_LIBOS_TEST_WORKERS` / `AGENT_LIBOS_TEST_DIST` to override defaults in CI.
Run the `gui` lane separately because it writes shared frontend build artifacts;
install GUI dependencies first with `npm --prefix gui install`.

Pytest cleans files created under the ignored `agent_outputs/` directory at the
end of each test session, while preserving anything that existed before the
session started. Use `--keep-agent-outputs` or set
`AGENT_LIBOS_KEEP_AGENT_OUTPUTS=1` when debugging generated files. To inspect or
clean already accumulated local output, run:

```bash
uv run python scripts/clean_agent_outputs.py
uv run python scripts/clean_agent_outputs.py --yes
```

Run a specific pytest lane with one of `unit`, `runtime`, `security`,
`self-evolution`, `providers`, or `benchmark`:

```bash
uv run python scripts/test_matrix.py --lane runtime
```

Useful smoke commands:

```bash
uv run agent-libos --help
uv run agent-libos checkpoint --help
uv run agent-libos skills --help
uv run agent-libos jsonrpc --help
uv run python experiments/run_benchmark.py --help
uv run python experiments/collect_metrics.py --help
```

Benchmark smoke:

```bash
uv run python experiments/run_benchmark.py --suite benchmarks/runtime_safety --runner agent_libos_full --limit 3 --output .benchmark_runs/docs-smoke
uv run python experiments/collect_metrics.py .benchmark_runs/docs-smoke
```

`.benchmark_runs/` is ignored and should not be committed.

## Real LLM Smoke

Real LLM paths are opt-in because tokens are valuable.

Configure the host environment, or pass an explicit env file to scripts that
offer one. The runtime LLM client does not implicitly read a workspace `.env`.

```bash
OPENAI_BASE_URL=https://example-openai-compatible-endpoint/v1
OPENAI_LANGUAGE_MODEL=your-model
OPENAI_API_KEY=...
AGENT_LIBOS_ALLOW_CUSTOM_LLM_BASE_URL=1
```

Useful optional variables:

- `OPENAI_API_MODE=responses|chat|auto`
- `OPENAI_TIMEOUT`
- `OPENAI_MAX_RETRIES`
- `OPENAI_STORE`
- `OPENAI_REASONING_EFFORT`
- `OPENAI_VERBOSITY`
- `OPENAI_SAFETY_IDENTIFIER`
- `OPENAI_PROMPT_CACHE_KEY`
- `OPENAI_PROMPT_CACHE_RETENTION=in-memory|24h`
- `OPENAI_RESPONSES_PREVIOUS_RESPONSE_ID=true|false`
- `OPENAI_PARALLEL_TOOL_CALLS=true|false`
- provider-specific `OPENAI_ENABLE_THINKING`

`OPENAI_BASE_URL` is optional for the OpenAI API. Custom OpenAI-compatible
endpoints require `AGENT_LIBOS_ALLOW_CUSTOM_LLM_BASE_URL=1` or an explicit
`allow_custom_base_url=True` client construction.

Official OpenAI Responses requests may also be configured with
privacy-preserving `safety_identifier`, prompt-cache routing fields, and
opt-in `previous_response_id` chaining. The runtime keeps `llm.store=False` and
Responses state chaining disabled by default; enable both only when retaining
provider-side response state is acceptable. These OpenAI-specific fields are
not sent to custom OpenAI-compatible endpoints. In the default stateless mode,
prior tool messages are sent back as ordinary bounded context text. Only an
active provider-side Responses chain sends tool outputs with a Responses
`call_id` as `function_call_output`. If a persisted tool-output record cannot be
expressed losslessly, Agent libOS omits `previous_response_id` for that request
and uses the plain-context fallback rather than continuing a corrupted state
chain.

Set `llm.parallel_tool_calls` or `OPENAI_PARALLEL_TOOL_CALLS=true` to let the
provider return multiple tool calls in one action-selection response. Agent
libOS dispatches that batch sequentially in one quantum; it does not run tools
concurrently.
Set `llm.auto_wait_on_empty_tool_calls: true` globally or on a specific LLM
profile only for providers that sometimes answer action-selection requests
without tool calls. When enabled, Agent libOS first preserves the existing
fallback JSON action parser; if the response still contains no valid action, it
synthesizes a `receive_process_messages` action with default arguments. The raw
LLM call record still stores the provider response with an empty `tool_calls`
list, and the synthetic wait listens for any unread process message.

Run a script smoke:

```bash
uv run python scripts/llm_write_goal_smoke.py
```

Run a benchmark smoke only with an explicit one-task limit:

```bash
uv run python experiments/run_benchmark.py --suite benchmarks/runtime_safety --runner agent_libos_full --llm real --limit 1 --output .benchmark_runs/real-smoke
```

Every runtime LLM action-selection call must persist an `llm_calls` row with
provider ids, model/API mode, usage, errors, full prompt, visible tools,
output, tool calls, reasoning metadata, raw responses, and bounded
observability envelopes. This default supports self-evolution training and
fine-tuning pipelines; deployments should disclose that retention and use in
their user agreement.

LLM providers are selected through host-configured named profiles. Processes
persist only `llm_profile_id`; the Runtime resolves that id for each quantum and
reads API keys from the profile's `api_key_env` environment variable. The
configured default profile preserves the existing `OPENAI_*` environment
behavior. Other named profiles do not inherit ambient provider/model
environment variables; set their profile fields explicitly when they should use
a non-default model or endpoint.

The GUI can also create user-level profiles without editing the project config.
Those profiles are host configuration, not runtime database state. Electron
stores them at `app.getPath("userData")/llm-profiles.json`; direct Python GUI
server runs use `%APPDATA%/Agent libOS/llm-profiles.json` on Windows,
`~/Library/Application Support/Agent libOS/llm-profiles.json` on macOS, and
`${XDG_CONFIG_HOME:-~/.config}/agent-libos/llm-profiles.json` on Linux unless
`agent-libos-gui-server --llm-profiles-file <path>` is provided. The file stores
only non-secret routing fields and the `api_key_env` variable name; never put
the API key value in it. If `base_url` is set and `allow_custom_base_url` is
explicitly false, the false value is persisted so the profile does not start
using a custom base URL after reload.

Example user/config profile fields for common OpenAI-compatible providers:

```yaml
llm:
  profiles:
    gpt-5.5:
      model: gpt-5.5
      api_key_env: OPENAI_API_KEY
    qwen3.7-max:
      base_url: https://dashscope-compatible.example/v1
      model: qwen3.7-max
      api_key_env: QWEN_API_KEY
      api_mode: chat
      allow_custom_base_url: true
    glm-5.2:
      base_url: https://open.bigmodel.example/api/paas/v4
      model: glm-5.2
      api_key_env: GLM_API_KEY
      api_mode: chat
      allow_custom_base_url: true
    kimi-k2.7-code:
      base_url: https://api.moonshot.example/v1
      model: kimi-k2.7-code
      api_key_env: KIMI_API_KEY
      api_mode: chat
      allow_custom_base_url: true
```

Set `llm.persist_full_io: false` in a config overlay, or construct a replacement
`AgentLibOSConfig`, to opt out of full prompt, visible tool schema, model
output, tool call, reasoning, and raw response persistence. The config
dataclasses are frozen, so do not mutate `DEFAULT_CONFIG` in place. When full
I/O persistence is disabled, the durable row keeps bounded previews, byte
counts, truncation flags, and hashes instead of the raw values.

The default remains `llm.persist_full_io: true` for deployments that use
complete LLM call records for self-evolution training or fine-tuning.

## Configuration Defaults

Non-secret runtime defaults live in `agent_libos.config.DEFAULT_CONFIG`.
`AgentLibOSConfig` uses Pydantic dataclass validation and fails fast when
numeric limits are negative, non-finite, inverted, or otherwise unsafe.
Product entrypoints read `config.yaml` from the project root when present, or
an explicit `--config <path>` overlay when provided. They do not auto-load a
`config.yaml` from the current working directory. Relative startup Runtime
Module paths in `config.modules.manifest_paths` resolve from the project root.
The loader starts from `DEFAULT_CONFIG`, recursively merges mapping fields,
replaces scalar/list/tuple fields, and then constructs a fresh
`AgentLibOSConfig`; it does not mutate `DEFAULT_CONFIG`.

Library and test code should keep passing explicit config objects when a custom
runtime is required:

```python
from agent_libos.config import load_config_file

config = load_config_file("config.yaml")
runtime = Runtime.open(config=config)
```

Current default groups include:

- runtime database and default ids,
- scheduler quantum, worker, drain, and shutdown limits,
- process resource budgets, usage accounting, and default cwd,
- LLM timeouts and provider compatibility knobs,
- tool limits and text encodings,
- filesystem and Object Memory size limits,
- Deno sandbox limits and JSR import allowlist,
- ObjectTask notification, owner-watch, and shutdown limits,
- shell policy allow/block lists,
- JSON-RPC endpoint manifest, timeout, and request/response limits,
- image registry limits,
- image commit limits,
- Object Memory and LLM context defaults,
- capability trusted issuer settings,
- GUI HTTP/event/request limits,
- checkpoint snapshot limits,
- Skill package source, trust, resource, and `SKILL.md` limits,
- trusted startup Runtime Module manifests, hash trust, and registration limits,
- launcher presets,
- script defaults.

Do not scatter magic numbers in implementation code when a value affects
runtime behavior, policy, persistence, or test reproducibility. Add a typed
config default instead.

## Manifest YAML

Runtime YAML manifests and `SKILL.md` frontmatter are parsed through
`agent_libos.utils.yaml_loader.load_yaml_mapping`, which uses PyYAML's safe
loader plus a duplicate-key check. YAML syntax follows PyYAML, while the
runtime schema validators still restrict which fields and value shapes each
manifest accepts. Duplicate mapping keys are rejected so authority-bearing
manifests fail closed instead of silently overwriting earlier declarations.

## Documentation Rules

README is the entrypoint. Detailed implementation documentation belongs in
`docs/`.

When behavior changes, update the relevant doc. If the change affects a runtime
invariant, update the machine-readable source in `tests/invariants.yaml` and
then sync `docs/invariants.md` in the same change. Do not describe future work
as current behavior. Paper-facing documentation should stay aligned with the
fixed title:
`Agent libOS: A Runtime Substrate for Capability-Controlled Self-Evolving LLM
Agents`.

Current behavior must not claim:

- Python JIT compatibility,
- direct external framework adapters as trusted boundaries,
- MCP Resources/Prompts, real GitHub/provider integrations that are not
  implemented,
- provider-level compensation for rollbackable external side effects,
- Skill activation as a capability grant.

`agent_libos_design_doc.md` remains a historical archive and can be stale.
`plan.md` is a dated paper roadmap; keep it useful for planning, but do not use
it as the implementation reference for current command syntax or runtime
behavior.

## Adding Runtime Code

Preserve the boundary:

- model-facing tools call primitives;
- primitives perform Capability authorization, policy, approval, events, and audit;
- providers perform host effects only after primitive authorization;
- JIT tools access libOS only through syscalls;
- Deno JIT tool execution runs with cached dependencies only. Validation is the
  phase that may resolve pinned allowlisted JSR imports and account for that
  dependency surface;
- Skills change visibility and prompt materialization only;
- self-evolution mechanisms such as Skills, JIT tools, image registration,
  process exec, checkpoint forks, child processes, and JSON-RPC endpoint
  visibility must not imply resource authority or additional resource budget;
- Runtime Modules are trusted startup TCB extensions; they may register tools,
  images, syscalls, and provider hooks but must not be treated as process
  capabilities;
- JSON-RPC remote calls use registered endpoints and primitive capabilities
  rather than model-supplied URLs or secrets. Calls perform an exact
  endpoint/method capability gate before loading manifest metadata or schemas;
- MCP remote tool calls likewise gate on `server_id` and `tool_id` before
  loading server metadata or input schemas;
- checkpoint restore is scoped and append-only outside reconstructable state;
  provider-classified external effects are report-only in v1.

Prefer existing managers and primitives over new side channels. If a new host
effect is needed, add or extend a primitive and provider interface rather than
calling the host directly from a tool.

## Dependencies

Add runtime dependencies with:

```bash
uv add <package>
```

Add development dependencies with:

```bash
uv add --dev <package>
```

Commit both `pyproject.toml` and `uv.lock` after dependency changes.
