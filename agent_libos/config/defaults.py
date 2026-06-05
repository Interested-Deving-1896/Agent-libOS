from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

ShellPolicyLevel = Literal[
    "always_deny",
    "allowlist_auto_else_ask",
    "blocklist_ask_else_auto",
    "always_allow",
]


@dataclass(frozen=True)
class ShellCommandRule:
    argv: tuple[str, ...]
    match: Literal["exact", "prefix"] = "exact"
    description: str = ""


@dataclass(frozen=True)
class RuntimeDefaults:
    local_store_target: str = "local"
    runtime_db_filename: str = ".agent_libos.sqlite"
    workspace_namespace: str = "workspace"
    default_image_id: str = "base-agent:v0"
    coding_image_id: str = "coding-agent:v0"
    default_human: str = "owner"
    terminal_channel: str = "terminal"
    run_until_idle_max_quanta: int = 25
    launcher_max_quanta: int = 40

    @property
    def default_human_resource(self) -> str:
        return f"human:{self.default_human}"

    @property
    def default_human_actor(self) -> str:
        return f"human:{self.default_human}"


@dataclass(frozen=True)
class SchedulerDefaults:
    max_quanta: int = 25
    poll_interval_s: float = 0.01


@dataclass(frozen=True)
class ProcessDefaults:
    max_tool_calls: int = 256
    max_child_processes: int = 16
    max_runtime_seconds: int | None = None
    max_materialized_tokens: int = 65_536
    default_goal_text: str = "Run agent process"
    default_working_directory: str = "."
    fork_budget_divisor: int = 2
    fork_min_tool_calls: int = 1
    fork_min_child_processes: int = 0


@dataclass(frozen=True)
class LLMDefaults:
    temperature: float = 0.2
    max_tokens: int = 65_536
    timeout_s: float = 60.0
    max_retries: int = 2
    api_mode: Literal["auto", "responses", "chat"] = "auto"
    store: bool = False
    compatibility_retry_attempts: int = 8
    action_repair_attempts: int = 2
    content_preview_chars: int = 500
    tool_arguments_preview_chars: int = 500
    json_instruction: str = "You must respond with a valid JSON object."
    fallback_status_codes: tuple[int, ...] = (404, 405)


@dataclass(frozen=True)
class ToolDefaults:
    version: str = "1.0.0"
    default_timeout_s: float = 30.0
    standard_timeout_s: float = 5.0
    interactive_timeout_s: float = 2.0
    default_text_encoding: str = "utf-8"
    filesystem_read_max_bytes: int = 65_536
    filesystem_read_hard_limit_bytes: int = 1_048_576
    directory_entry_limit: int = 1_024
    directory_entry_hard_limit: int = 10_000
    memory_payload_chars: int = 12_000
    memory_payload_hard_limit_chars: int = 200_000
    object_file_max_bytes: int = 1_048_576
    object_file_hard_limit_bytes: int = 10_485_760
    shell_timeout_s: float = 30.0
    sandbox_timeout_s: float = 5.0
    deno_executable: str = "deno"
    deno_timeout_s: float = 5.0
    deno_max_rpc_calls: int = 64
    deno_max_stdout_bytes: int = 1_000_000
    deno_max_stderr_bytes: int = 100_000
    deno_jsr_allowlist: tuple[str, ...] = (
        "@std/assert",
        "@std/collections",
        "@std/encoding",
        "@std/path",
        "@std/yaml",
    )
    static_tool_id_digest_chars: int = 16
    approval_preview_chars: int = 256
    clock_timezone: str = "UTC"
    max_sleep_seconds: float = 60.0
    sleep_timeout_grace_s: float = 5.0

    @property
    def sleep_tool_timeout_s(self) -> float:
        return self.max_sleep_seconds + self.sleep_timeout_grace_s


@dataclass(frozen=True)
class ShellDefaults:
    policy_capability_key: str = "shell_policy_level"
    policy_resource: str = "shell:*"
    default_policy_level: ShellPolicyLevel = "allowlist_auto_else_ask"
    always_deny_level: ShellPolicyLevel = "always_deny"
    allowlist_auto_else_ask_level: ShellPolicyLevel = "allowlist_auto_else_ask"
    blocklist_ask_else_auto_level: ShellPolicyLevel = "blocklist_ask_else_auto"
    always_allow_level: ShellPolicyLevel = "always_allow"
    high_risk_level: ShellPolicyLevel = "always_allow"
    timeout_hard_limit_s: float = 300.0
    max_stdout_chars: int = 32_000
    max_stderr_chars: int = 32_000
    stdout_hard_limit_chars: int = 200_000
    stderr_hard_limit_chars: int = 200_000
    whitelist: tuple[ShellCommandRule, ...] = (
        ShellCommandRule(("git", "status")),
        ShellCommandRule(("git", "status", "--short")),
        ShellCommandRule(("git", "branch", "--show-current")),
        ShellCommandRule(("git", "rev-parse", "--show-toplevel")),
        ShellCommandRule(("git", "diff", "--stat")),
        ShellCommandRule(("python", "--version")),
        ShellCommandRule(("python3", "--version")),
        ShellCommandRule(("uv", "--version")),
    )
    blacklist: tuple[ShellCommandRule, ...] = (
        ShellCommandRule(("cmd",), match="prefix"),
        ShellCommandRule(("powershell",), match="prefix"),
        ShellCommandRule(("pwsh",), match="prefix"),
        ShellCommandRule(("sh",), match="prefix"),
        ShellCommandRule(("bash",), match="prefix"),
        ShellCommandRule(("zsh",), match="prefix"),
        ShellCommandRule(("fish",), match="prefix"),
        ShellCommandRule(("python",), match="prefix"),
        ShellCommandRule(("python3",), match="prefix"),
        ShellCommandRule(("py",), match="prefix"),
        ShellCommandRule(("node",), match="prefix"),
        ShellCommandRule(("npm",), match="prefix"),
        ShellCommandRule(("npx",), match="prefix"),
        ShellCommandRule(("yarn",), match="prefix"),
        ShellCommandRule(("pnpm",), match="prefix"),
        ShellCommandRule(("uv",), match="prefix"),
        ShellCommandRule(("pip",), match="prefix"),
        ShellCommandRule(("pip3",), match="prefix"),
        ShellCommandRule(("ruby",), match="prefix"),
        ShellCommandRule(("perl",), match="prefix"),
        ShellCommandRule(("php",), match="prefix"),
        ShellCommandRule(("java",), match="prefix"),
        ShellCommandRule(("cargo",), match="prefix"),
        ShellCommandRule(("docker",), match="prefix"),
        ShellCommandRule(("kubectl",), match="prefix"),
        ShellCommandRule(("ssh",), match="prefix"),
        ShellCommandRule(("scp",), match="prefix"),
        ShellCommandRule(("sftp",), match="prefix"),
        ShellCommandRule(("curl",), match="prefix"),
        ShellCommandRule(("wget",), match="prefix"),
        ShellCommandRule(("nc",), match="prefix"),
        ShellCommandRule(("ncat",), match="prefix"),
        ShellCommandRule(("netcat",), match="prefix"),
        ShellCommandRule(("rm",), match="prefix"),
        ShellCommandRule(("del",), match="prefix"),
        ShellCommandRule(("rmdir",), match="prefix"),
        ShellCommandRule(("remove-item",), match="prefix"),
        ShellCommandRule(("move-item",), match="prefix"),
        ShellCommandRule(("copy-item",), match="prefix"),
        ShellCommandRule(("chmod",), match="prefix"),
        ShellCommandRule(("chown",), match="prefix"),
        ShellCommandRule(("icacls",), match="prefix"),
        ShellCommandRule(("reg",), match="prefix"),
        ShellCommandRule(("regedit",), match="prefix"),
        ShellCommandRule(("taskkill",), match="prefix"),
        ShellCommandRule(("sudo",), match="prefix"),
        ShellCommandRule(("su",), match="prefix"),
        ShellCommandRule(("runas",), match="prefix"),
    )


@dataclass(frozen=True)
class ImageDefaults:
    registry_resource: str = "image:*"
    id_max_chars: int = 128
    name_max_chars: int = 128
    version_max_chars: int = 64
    max_default_tools: int = 128
    max_required_capabilities: int = 64
    yaml_max_bytes: int = 262_144
    yaml_hard_limit_bytes: int = 1_048_576


@dataclass(frozen=True)
class ObjectMemoryDefaults:
    object_schema_version: str = "1"
    materialize_budget_tokens: int = 8_000
    query_limit: int = 50
    context_policy: str = "plan_first"
    metadata_sensitivity: str = "normal"
    metadata_retention_policy: str = "default"
    process_namespace_prefix: str = "process"


@dataclass(frozen=True)
class LLMContextDefaults:
    policy: str = "llm_context_object"
    schema_version: int = 1
    object_name_prefix: str = "llm_context"
    recent_event_limit: int = 20


@dataclass(frozen=True)
class CheckpointDefaults:
    snapshot_version: int = 1
    list_limit: int = 100
    payload_capture_limit_bytes: int = 1_048_576
    snapshot_hard_limit_bytes: int = 16_777_216
    diff_preview_items: int = 25
    auto_high_risk_checkpoint: bool = False


@dataclass(frozen=True)
class SkillDefaults:
    schema_version: int = 1
    registry_resource: str = "skill:*"
    trust_resource: str = "skill_trust:*"
    global_dirs: tuple[str, ...] = ("~/.agent-libos/skills",)
    workspace_dirs: tuple[str, ...] = ("skills", ".agent_libos/skills")
    trusted_global_sha256: tuple[str, ...] = ()
    global_requires_trust: bool = True
    manifest_max_bytes: int = 262_144
    manifest_hard_limit_bytes: int = 1_048_576
    max_prompt_instruction_chars: int = 8_000
    max_jit_source_chars: int = 64_000
    discover_limit: int = 100
    id_max_chars: int = 128
    name_max_chars: int = 128
    version_max_chars: int = 64
    max_tools: int = 128
    max_actions: int = 128
    max_jit_tools: int = 32
    max_required_capabilities: int = 64


@dataclass(frozen=True)
class LauncherDefaults:
    permission_presets: tuple[str, ...] = ("read-only", "edit", "full")
    default_permission_preset: str = "edit"
    read_only_preset: str = "read-only"
    edit_preset: str = "edit"
    full_preset: str = "full"


@dataclass(frozen=True)
class ScriptDefaults:
    ask_file_max_bytes: int = 65_536
    ask_file_max_quanta: int = 6
    document_summary_max_bytes: int = 65_536
    document_summary_max_read_bytes: int = 1_048_576
    document_summary_max_quanta: int = 10
    document_context_min_tokens: int = 8_000
    document_context_slack_tokens: int = 12_000
    document_context_max_tokens: int = 120_000
    object_copy_max_quanta: int = 5
    llm_write_smoke_max_quanta: int = 5
    clock_demo_iterations: int = 3
    clock_demo_interval_s: float = 0.2
    clock_demo_timezone: str = "Asia/Shanghai"
    chat_max_turns: int = 20
    chat_context_tokens: int = 64_000
    chat_quanta_per_turn: int = 5
    chat_quanta_overhead: int = 8


@dataclass(frozen=True)
class AgentLibOSConfig:
    runtime: RuntimeDefaults = field(default_factory=RuntimeDefaults)
    scheduler: SchedulerDefaults = field(default_factory=SchedulerDefaults)
    process: ProcessDefaults = field(default_factory=ProcessDefaults)
    llm: LLMDefaults = field(default_factory=LLMDefaults)
    tools: ToolDefaults = field(default_factory=ToolDefaults)
    shell: ShellDefaults = field(default_factory=ShellDefaults)
    image: ImageDefaults = field(default_factory=ImageDefaults)
    memory: ObjectMemoryDefaults = field(default_factory=ObjectMemoryDefaults)
    llm_context: LLMContextDefaults = field(default_factory=LLMContextDefaults)
    checkpoint: CheckpointDefaults = field(default_factory=CheckpointDefaults)
    skills: SkillDefaults = field(default_factory=SkillDefaults)
    launcher: LauncherDefaults = field(default_factory=LauncherDefaults)
    scripts: ScriptDefaults = field(default_factory=ScriptDefaults)


DEFAULT_CONFIG = AgentLibOSConfig()
