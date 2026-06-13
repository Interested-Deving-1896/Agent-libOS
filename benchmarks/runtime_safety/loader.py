from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from agent_libos.utils.yaml_loader import load_yaml_mapping
from benchmarks.runtime_safety.models import BenchmarkTask, BenchmarkValidationError, VALID_EFFECT_TYPES

REQUIRED_FIELDS = {
    "id",
    "title",
    "goal",
    "workspace",
    "attack_class",
    "allowed_effects",
    "forbidden_effects",
    "success_oracle",
    "safety_oracle",
}
_TASK_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_]*$")


def load_tasks(suite_root: str | Path) -> list[BenchmarkTask]:
    root = Path(suite_root)
    tasks_dir = root / "tasks"
    if not tasks_dir.exists():
        raise BenchmarkValidationError(f"benchmark tasks directory does not exist: {tasks_dir}")
    tasks = [load_task_file(path) for path in sorted(tasks_dir.glob("*.yaml"))]
    seen: set[str] = set()
    duplicates: list[str] = []
    for task in tasks:
        if task.id in seen:
            duplicates.append(task.id)
        seen.add(task.id)
    if duplicates:
        raise BenchmarkValidationError(f"duplicate benchmark task ids: {sorted(set(duplicates))}")
    return tasks


def load_task_file(path: str | Path) -> BenchmarkTask:
    source = Path(path)
    data = load_yaml_mapping(source.read_text(encoding="utf-8"))
    _validate_required(data, source)
    schema_version = data.get("schema_version", 0)
    if schema_version != 0:
        raise BenchmarkValidationError(f"{source}: unsupported schema_version {schema_version!r}")
    task_id = _string_field(data, "id", source)
    if not _TASK_ID_RE.match(task_id):
        raise BenchmarkValidationError(f"{source}: id must be lowercase snake_case, got {task_id!r}")
    allowed = _validate_effect_list(data.get("allowed_effects"), source, "allowed_effects")
    forbidden = _validate_effect_list(data.get("forbidden_effects"), source, "forbidden_effects")
    success_oracle = _validate_mapping_list(data.get("success_oracle"), source, "success_oracle")
    safety_oracle = _validate_mapping_list(data.get("safety_oracle"), source, "safety_oracle")
    mock_actions = _validate_mapping_list(data.get("mock_actions", []), source, "mock_actions")
    for index, action in enumerate(mock_actions):
        if not isinstance(action.get("action"), str) or not action["action"]:
            raise BenchmarkValidationError(f"{source}: mock_actions[{index}] requires non-empty action")
        _validate_action_paths(action, source, index)
    return BenchmarkTask(
        id=task_id,
        title=_string_field(data, "title", source),
        goal=_string_field(data, "goal", source),
        workspace=_safe_relative_path(_string_field(data, "workspace", source), source, "workspace"),
        attack_class=_string_field(data, "attack_class", source),
        allowed_effects=allowed,
        forbidden_effects=forbidden,
        success_oracle=success_oracle,
        safety_oracle=safety_oracle,
        schema_version=0,
        setup=_optional_mapping(data.get("setup", {}), source, "setup"),
        capabilities=_optional_mapping(data.get("capabilities", {}), source, "capabilities"),
        policy=_optional_mapping(data.get("policy", {}), source, "policy"),
        human_responses=_validate_mapping_list(data.get("human_responses", []), source, "human_responses"),
        expected_audit=_validate_mapping_list(data.get("expected_audit", []), source, "expected_audit"),
        mock_actions=mock_actions,
        notes=data.get("notes") if isinstance(data.get("notes"), str) else None,
        source_path=source,
    )


def _validate_required(data: dict[str, Any], source: Path) -> None:
    missing = sorted(REQUIRED_FIELDS - set(data))
    if missing:
        raise BenchmarkValidationError(f"{source}: missing required fields: {missing}")


def _string_field(data: dict[str, Any], field: str, source: Path) -> str:
    value = data.get(field)
    if not isinstance(value, str) or not value.strip():
        raise BenchmarkValidationError(f"{source}: {field} must be a non-empty string")
    return value.strip()


def _optional_mapping(value: Any, source: Path, field: str) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise BenchmarkValidationError(f"{source}: {field} must be a mapping")
    return value


def _validate_mapping_list(value: Any, source: Path, field: str) -> list[dict[str, Any]]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise BenchmarkValidationError(f"{source}: {field} must be a list")
    result: list[dict[str, Any]] = []
    for index, item in enumerate(value):
        if not isinstance(item, dict):
            raise BenchmarkValidationError(f"{source}: {field}[{index}] must be a mapping")
        result.append(dict(item))
    return result


def _validate_effect_list(value: Any, source: Path, field: str) -> list[dict[str, Any]]:
    effects = _validate_mapping_list(value, source, field)
    for index, effect in enumerate(effects):
        effect_type = effect.get("type")
        if effect_type not in VALID_EFFECT_TYPES:
            raise BenchmarkValidationError(
                f"{source}: {field}[{index}].type must be one of {sorted(VALID_EFFECT_TYPES)}, got {effect_type!r}"
            )
        if str(effect_type).startswith("filesystem."):
            if "path" not in effect:
                raise BenchmarkValidationError(f"{source}: {field}[{index}] requires path")
            effect["path"] = _safe_relative_path(str(effect["path"]), source, f"{field}[{index}].path")
        if effect_type == "shell.exec":
            _validate_argv(effect.get("argv"), source, f"{field}[{index}].argv")
        if str(effect_type).startswith("object."):
            namespace = effect.get("namespace")
            if namespace is not None and (not isinstance(namespace, str) or ".." in namespace.replace("\\", "/").split("/")):
                raise BenchmarkValidationError(f"{source}: {field}[{index}].namespace is unsafe")
        if effect_type in {"process.spawn", "process.fork", "process.exec"} and "image" in effect:
            if not isinstance(effect["image"], str) or not effect["image"]:
                raise BenchmarkValidationError(f"{source}: {field}[{index}].image must be a string")
        if effect_type == "skill.activate":
            _validate_non_empty_string(effect, "skill_id", source, f"{field}[{index}].skill_id")
        if effect_type == "jit.register":
            _validate_non_empty_string(effect, "tool", source, f"{field}[{index}].tool")
        if effect_type in {"image.register", "image.commit"}:
            _validate_non_empty_string(effect, "image", source, f"{field}[{index}].image")
        if effect_type in {"checkpoint.create", "checkpoint.fork"}:
            _validate_non_empty_string(effect, "checkpoint", source, f"{field}[{index}].checkpoint", required=False)
        if effect_type == "jsonrpc.call":
            _validate_non_empty_string(effect, "endpoint", source, f"{field}[{index}].endpoint")
            _validate_non_empty_string(effect, "method", source, f"{field}[{index}].method")
    return effects


def _validate_action_paths(action: dict[str, Any], source: Path, index: int) -> None:
    name = str(action.get("action"))
    if name in {"read_text_file", "write_text_file", "delete_file", "delete_directory", "read_directory", "write_directory"}:
        if "path" not in action:
            raise BenchmarkValidationError(f"{source}: mock_actions[{index}] {name} requires path")
        action["path"] = _safe_relative_path(str(action["path"]), source, f"mock_actions[{index}].path")
    if name == "run_shell_command":
        _validate_argv(action.get("argv"), source, f"mock_actions[{index}].argv")
    effects = action.get("benchmark_effects")
    if effects is not None:
        _validate_effect_list(effects, source, f"mock_actions[{index}].benchmark_effects")
    checkpoint_ref = action.get("checkpoint_ref")
    if checkpoint_ref is not None and (not isinstance(checkpoint_ref, str) or not checkpoint_ref.strip()):
        raise BenchmarkValidationError(f"{source}: mock_actions[{index}].checkpoint_ref must be a non-empty string")


def _safe_relative_path(value: str, source: Path, field: str) -> str:
    raw = value.strip().replace("\\", "/")
    if not raw:
        raise BenchmarkValidationError(f"{source}: {field} must be non-empty")
    if raw.startswith("/") or raw.startswith("~") or re.match(r"^[A-Za-z]:", raw):
        raise BenchmarkValidationError(f"{source}: {field} must be workspace-relative: {value!r}")
    parts = [part for part in raw.split("/") if part not in {"", "."}]
    if any(part == ".." for part in parts):
        raise BenchmarkValidationError(f"{source}: {field} may not escape workspace: {value!r}")
    return "/".join(parts) if parts else "."


def _validate_argv(value: Any, source: Path, field: str) -> None:
    if not isinstance(value, list) or not value:
        raise BenchmarkValidationError(f"{source}: {field} must be a non-empty list")
    for index, item in enumerate(value):
        if not isinstance(item, str):
            raise BenchmarkValidationError(f"{source}: {field}[{index}] must be a string")
        if index == 0 and not item.strip():
            raise BenchmarkValidationError(f"{source}: {field}[0] must be non-empty")
        if "\x00" in item:
            raise BenchmarkValidationError(f"{source}: {field}[{index}] may not contain NUL")


def _validate_non_empty_string(
    mapping: dict[str, Any],
    key: str,
    source: Path,
    field: str,
    *,
    required: bool = True,
) -> None:
    if key not in mapping:
        if required:
            raise BenchmarkValidationError(f"{source}: {field} is required")
        return
    if not isinstance(mapping[key], str) or not mapping[key].strip():
        raise BenchmarkValidationError(f"{source}: {field} must be a non-empty string")
