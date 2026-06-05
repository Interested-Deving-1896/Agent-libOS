from __future__ import annotations

import asyncio
import json
import os
import shutil
import time
from pathlib import Path
from typing import Any

from agent_libos import Runtime
from agent_libos.capability.manager import CapabilityManager
from agent_libos.config import DEFAULT_CONFIG
from agent_libos.llm.client import LLMClient, LLMCompletion
from agent_libos.models import CapabilityRight, ObjectMetadata, ObjectRight, ObjectType, ProcessStatus
from agent_libos.storage import SQLiteStore
from agent_libos.substrate import LocalResourceProviderSubstrate
from agent_libos.utils.serde import to_jsonable
from benchmarks.runtime_safety.fixtures import prepare_workspace, safe_workspace_path
from benchmarks.runtime_safety.models import BenchmarkResult, BenchmarkTask, EffectRecord, TaskRun
from benchmarks.runtime_safety.oracle import classify_effects, safety_summary

RUNNER_NAMES = (
    "direct_tool_wrapper",
    "confirmation_wrapper",
    "sandbox_only",
    "agent_libos_full",
    "no_primitive_approval",
    "no_audit_linkage",
    "no_namespace_isolation",
    "no_fork_attenuation",
)
AGENT_LIBOS_RUNNERS = {
    "agent_libos_full",
    "no_primitive_approval",
    "no_audit_linkage",
    "no_namespace_isolation",
    "no_fork_attenuation",
}
_TERMINAL_STATUSES = {ProcessStatus.EXITED, ProcessStatus.FAILED, ProcessStatus.KILLED}


class PlannedActionClient:
    def __init__(self, actions: list[dict[str, Any]]):
        self.actions = [dict(action) for action in actions]
        self.calls = 0

    def complete_action(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> LLMCompletion:
        self.calls += 1
        action = self.actions.pop(0) if self.actions else {"action": "process_exit", "payload": {"done": True}}
        name = str(action["action"])
        args = {key: value for key, value in action.items() if key != "action"}
        return LLMCompletion(
            content="planned benchmark action",
            tool_calls=[{"id": f"bench_{self.calls}", "name": name, "arguments": json.dumps(args)}],
            api="mock",
            model="planned-action-client",
            usage={"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            reasoning={"summary": "deterministic benchmark plan"},
        )


def run_suite(
    tasks: list[BenchmarkTask],
    suite_root: str | Path,
    output_dir: str | Path,
    *,
    runners: list[str],
    llm_mode: str = "mock",
    max_quanta: int | None = None,
) -> list[TaskRun]:
    runs: list[TaskRun] = []
    for runner in runners:
        if runner not in RUNNER_NAMES:
            raise ValueError(f"unknown benchmark runner: {runner}")
        for task in tasks:
            runs.append(run_task(task, suite_root, output_dir, runner=runner, llm_mode=llm_mode, max_quanta=max_quanta))
    return runs


def run_task(
    task: BenchmarkTask,
    suite_root: str | Path,
    output_dir: str | Path,
    *,
    runner: str,
    llm_mode: str = "mock",
    max_quanta: int | None = None,
) -> TaskRun:
    if runner in AGENT_LIBOS_RUNNERS:
        return _run_agent_libos_task(task, suite_root, output_dir, runner=runner, llm_mode=llm_mode, max_quanta=max_quanta)
    if llm_mode == "real":
        raise ValueError("real LLM mode is only supported for Agent libOS runners")
    return _run_wrapper_task(task, suite_root, output_dir, runner=runner)


def _run_wrapper_task(
    task: BenchmarkTask,
    suite_root: str | Path,
    output_dir: str | Path,
    *,
    runner: str,
) -> TaskRun:
    started = time.perf_counter()
    workspace = prepare_workspace(task, suite_root, output_dir, runner)
    state: dict[str, Any] = {
        "exited": False,
        "errors": [],
        "approvals": 0,
        "memory": _setup_wrapper_memory(task),
    }
    effects: list[EffectRecord] = []
    for action in task.mock_actions:
        if action.get("action") == "process_exit":
            state["exited"] = True
            continue
        effect = _effect_from_action(task, runner, action)
        if effect is None:
            continue
        if runner == "confirmation_wrapper" and _is_side_effect(effect):
            state["approvals"] += 1
            approved = str(task.policy.get("confirmation_default_decision", "approve")).lower() != "deny"
            if not approved:
                effect.performed = False
                effect.denied = True
                effect.error = "confirmation rejected"
                effects.append(effect)
                continue
        try:
            _perform_wrapper_action(action, workspace, state, effect, runner=runner)
        except Exception as exc:  # benchmark runner records failures instead of aborting the suite
            effect.performed = False
            effect.denied = _looks_like_denial(str(exc))
            effect.error = str(exc)
            state["errors"].append(str(exc))
        effects.append(effect)
    classify_effects(task, effects)
    safety = safety_summary(task, effects)
    success = _evaluate_success(task, workspace, state)
    wall_time = time.perf_counter() - started
    result = BenchmarkResult(
        task_id=task.id,
        runner=runner,
        attack_class=task.attack_class,
        ok=success and safety["safety_passed"],
        task_success=success,
        safety_passed=bool(safety["safety_passed"]),
        unknown_effects=int(safety["unknown_effects"]),
        forbidden_performed=int(safety["forbidden_performed"]),
        approval_count=int(state["approvals"]),
        tool_calls=len(task.mock_actions),
        primitive_calls=0,
        llm_tokens=0,
        wall_time_s=wall_time,
        audit_records=0,
        audit_completeness=0.0,
        errors=list(state["errors"]),
        workspace=str(workspace),
        metadata={"simulated_shell": True, "fixture_workspace": str(workspace)},
    )
    return TaskRun(result=result, effects=effects)


def _run_agent_libos_task(
    task: BenchmarkTask,
    suite_root: str | Path,
    output_dir: str | Path,
    *,
    runner: str,
    llm_mode: str,
    max_quanta: int | None,
) -> TaskRun:
    started = time.perf_counter()
    workspace = prepare_workspace(task, suite_root, output_dir, runner)
    run_root = Path(output_dir) / "agent_libos" / runner / task.id
    if run_root.exists():
        shutil.rmtree(run_root)
    run_root.mkdir(parents=True, exist_ok=True)
    db_path = run_root / "runtime.sqlite"
    client = PlannedActionClient(task.mock_actions) if llm_mode == "mock" else LLMClient.from_env()
    runtime = Runtime(SQLiteStore(db_path), llm_client=client, substrate=LocalResourceProviderSubstrate(workspace))
    errors: list[str] = []
    try:
        pid = runtime.process.spawn(image="review-agent:v0", goal=task.goal)
        setup_objects = _setup_runtime_memory(task, runtime, runner, pid)
        _grant_task_capabilities(task, runtime, pid, runner, setup_objects)
        selected_quanta = max_quanta or max(len(task.mock_actions) + 4, 4)
        results = runtime.run_until_idle(
            max_quanta=selected_quanta,
            human_auto_approve=bool(task.policy.get("human_auto_approve", False)),
            human_auto_policy=task.policy.get("human_auto_policy"),
            human_auto_answer=task.policy.get("human_auto_answer"),
        )
        process = runtime.process.get(pid)
        effects = _effects_from_runtime_results(task, runner, results)
        classify_effects(task, effects)
        safety = safety_summary(task, effects)
        llm_calls = runtime.store.list_llm_calls(pid=pid)
        tokens = sum(int(call.usage.get("total_tokens") or 0) for call in llm_calls)
        audit = runtime.audit.trace()
        primitive_calls = len([record for record in audit if record.action.startswith("primitive.")])
        approvals = len([record for record in audit if record.action in {"human.query", "human.approve", "human.reject"}])
        audit_records = 0 if runner == "no_audit_linkage" else len(audit)
        audit_completeness = _audit_completeness(runner, effects, audit_records)
        state = {"exited": process.status in _TERMINAL_STATUSES, "errors": errors}
        success = _evaluate_success(task, workspace, state)
        wall_time = time.perf_counter() - started
        result = BenchmarkResult(
            task_id=task.id,
            runner=runner,
            attack_class=task.attack_class,
            ok=success and bool(safety["safety_passed"]),
            task_success=success,
            safety_passed=bool(safety["safety_passed"]),
            unknown_effects=int(safety["unknown_effects"]),
            forbidden_performed=int(safety["forbidden_performed"]),
            approval_count=approvals,
            tool_calls=len([record for record in audit if record.action == "tool.call"]),
            primitive_calls=primitive_calls,
            llm_tokens=tokens,
            wall_time_s=wall_time,
            audit_records=audit_records,
            audit_completeness=audit_completeness,
            errors=errors,
            workspace=str(workspace),
            metadata={
                "pid": pid,
                "db": str(db_path),
                "llm_mode": llm_mode,
                "llm_calls": len(llm_calls),
                "process_status": process.status.value,
                "setup_object_oids": [item["oid"] for item in setup_objects],
            },
        )
        return TaskRun(result=result, effects=effects)
    except Exception as exc:
        errors.append(str(exc))
        wall_time = time.perf_counter() - started
        result = BenchmarkResult(
            task_id=task.id,
            runner=runner,
            attack_class=task.attack_class,
            ok=False,
            task_success=False,
            safety_passed=False,
            unknown_effects=0,
            forbidden_performed=0,
            approval_count=0,
            tool_calls=0,
            primitive_calls=0,
            llm_tokens=0,
            wall_time_s=wall_time,
            audit_records=len(runtime.audit.trace()),
            audit_completeness=0.0,
            errors=errors,
            workspace=str(workspace),
        )
        return TaskRun(result=result, effects=[])
    finally:
        runtime.close()


def _setup_wrapper_memory(task: BenchmarkTask) -> dict[tuple[str, str], Any]:
    memory: dict[tuple[str, str], Any] = {}
    for item in (task.setup or {}).get("memory_objects", []) or []:
        if isinstance(item, dict):
            memory[(str(item.get("namespace") or "process"), str(item.get("name") or ""))] = item.get("payload")
    return memory


def _setup_runtime_memory(
    task: BenchmarkTask,
    runtime: Runtime,
    runner: str,
    target_pid: str,
) -> list[dict[str, Any]]:
    setup_objects: list[dict[str, Any]] = []
    memory_objects = (task.setup or {}).get("memory_objects", []) or []
    if not memory_objects:
        return setup_objects
    owner = runtime.process.spawn(image="base-agent:v0", goal=f"setup memory for {task.id}")
    for item in memory_objects:
        if not isinstance(item, dict):
            continue
        namespace = str(item.get("namespace") or runtime.memory.process_namespace(owner))
        _ensure_namespace_chain(runtime, owner, namespace)
        handle = runtime.memory.create_object(
            pid=owner,
            object_type=str(item.get("type") or "observation"),
            namespace=namespace,
            name=str(item.get("name") or "object"),
            payload=item.get("payload"),
            metadata=ObjectMetadata(title=f"benchmark setup object {task.id}", tags=["benchmark", "setup"]),
            immutable=bool(item.get("immutable", True)),
        )
        setup_objects.append({"oid": handle.oid, "namespace": namespace, "name": str(item.get("name") or "object")})
        if runner == "no_namespace_isolation" or bool(item.get("grant_to_process", False)):
            runtime.capability.grant(
                subject=target_pid,
                resource=f"object_namespace:{namespace}",
                rights=["read"],
                issued_by=f"benchmark:{runner}",
            )
            runtime.capability.grant(
                subject=target_pid,
                resource=f"object:{handle.oid}",
                rights=[ObjectRight.READ.value, ObjectRight.MATERIALIZE.value],
                issued_by=f"benchmark:{runner}",
            )
    runtime.process.exit(owner, message="benchmark setup complete")
    return setup_objects


def _ensure_namespace_chain(runtime: Runtime, pid: str, namespace: str) -> None:
    current = ""
    for part in namespace.replace("\\", "/").strip("/").split("/"):
        current = part if not current else f"{current}/{part}"
        if runtime.store.get_namespace(current) is not None:
            continue
        runtime.memory.create_namespace(pid, current)


def _grant_task_capabilities(
    task: BenchmarkTask,
    runtime: Runtime,
    pid: str,
    runner: str,
    setup_objects: list[dict[str, Any]],
) -> None:
    capabilities = task.capabilities or {}
    filesystem = capabilities.get("filesystem") if isinstance(capabilities.get("filesystem"), dict) else {}
    for right in ("read", "write", "delete"):
        for path in filesystem.get(right, []) or []:
            resource = _filesystem_resource(runtime, str(path))
            runtime.capability.grant(
                subject=pid,
                resource=resource,
                rights=[right],
                issued_by=f"benchmark:{task.id}",
            )
    shell = capabilities.get("shell") if isinstance(capabilities.get("shell"), dict) else {}
    if shell.get("policy"):
        runtime.shell.grant_policy(pid, str(shell["policy"]), issued_by=f"benchmark:{task.id}")
    human = capabilities.get("human") if isinstance(capabilities.get("human"), list) else []
    for right in human:
        runtime.capability.grant(pid, DEFAULT_CONFIG.runtime.default_human_resource, [str(right)], issued_by=f"benchmark:{task.id}")
    if runner == "no_primitive_approval":
        runtime.capability.grant(pid, runtime.filesystem.workspace_resource(), ["read", "write", "delete"], issued_by="benchmark:ablation")
        runtime.shell.grant_policy(pid, "always_allow", issued_by="benchmark:ablation")
        for item in setup_objects:
            runtime.capability.grant(pid, f"object_namespace:{item['namespace']}", ["read"], issued_by="benchmark:ablation")
            runtime.capability.grant(pid, f"object:{item['oid']}", ["read", "materialize"], issued_by="benchmark:ablation")
    if runner == "no_fork_attenuation":
        runtime.capability.grant(pid, runtime.filesystem.workspace_resource(), ["read", "write"], issued_by="benchmark:ablation")


def _filesystem_resource(runtime: Runtime, path: str) -> str:
    normalized = path.replace("\\", "/").strip()
    if "*" in normalized:
        return runtime.filesystem.resource_for(normalized)
    return runtime.filesystem.resource_for_path(normalized)


def _perform_wrapper_action(
    action: dict[str, Any],
    workspace: Path,
    state: dict[str, Any],
    effect: EffectRecord,
    *,
    runner: str,
) -> None:
    name = str(action.get("action"))
    if name == "read_text_file":
        content = safe_workspace_path(workspace, str(action["path"])).read_text(encoding=str(action.get("encoding") or "utf-8"))
        effect.metadata["bytes_read"] = len(content.encode("utf-8"))
    elif name == "write_text_file":
        target = safe_workspace_path(workspace, str(action["path"]))
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(str(action.get("content", "")), encoding=str(action.get("encoding") or "utf-8"), newline="\n")
        effect.metadata["bytes_written"] = len(str(action.get("content", "")).encode("utf-8"))
    elif name in {"delete_file", "delete_directory"}:
        target = safe_workspace_path(workspace, str(action["path"]))
        if target.is_dir():
            for child in sorted(target.rglob("*"), reverse=True):
                if child.is_file():
                    child.unlink()
                elif child.is_dir():
                    child.rmdir()
            target.rmdir()
        elif target.exists():
            target.unlink()
    elif name == "run_shell_command":
        effect.simulated = True
        effect.metadata["returncode"] = 0
    elif name == "read_memory_object":
        key = (str(action.get("namespace") or "process"), str(action.get("name") or ""))
        if key not in state["memory"]:
            raise PermissionError(f"object not found or inaccessible: {key}")
    elif name in {"create_memory_object", "append_memory_object"}:
        key = (str(action.get("namespace") or "process"), str(action.get("name") or "object"))
        state["memory"][key] = action.get("payload", action.get("entry"))
    elif name in {"spawn_child_process", "fork_child_process", "exec_process", "ask_human", "request_permission"}:
        effect.simulated = runner in {"direct_tool_wrapper", "confirmation_wrapper", "sandbox_only"}
    else:
        effect.simulated = True


def _effects_from_runtime_results(task: BenchmarkTask, runner: str, results: list[Any]) -> list[EffectRecord]:
    effects: list[EffectRecord] = []
    for item in results:
        if not isinstance(item, dict):
            continue
        action = item.get("action")
        if not isinstance(action, dict):
            continue
        effect = _effect_from_action(task, runner, action)
        if effect is None:
            continue
        result = item.get("result") if isinstance(item.get("result"), dict) else {}
        effect.performed = bool(result.get("ok"))
        effect.denied = not effect.performed and _looks_like_denial(str(result.get("error") or ""))
        effect.error = None if effect.performed else str(result.get("error") or "")
        effects.append(effect)
    return effects


def _effect_from_action(task: BenchmarkTask, runner: str, action: dict[str, Any]) -> EffectRecord | None:
    name = str(action.get("action"))
    if name == "read_text_file":
        return EffectRecord(task_id=task.id, runner=runner, type="filesystem.read", performed=True, path=str(action.get("path")))
    if name == "write_text_file":
        return EffectRecord(task_id=task.id, runner=runner, type="filesystem.write", performed=True, path=str(action.get("path")))
    if name in {"delete_file", "delete_directory"}:
        return EffectRecord(task_id=task.id, runner=runner, type="filesystem.delete", performed=True, path=str(action.get("path")))
    if name == "run_shell_command":
        return EffectRecord(task_id=task.id, runner=runner, type="shell.exec", performed=True, argv=[str(item) for item in action.get("argv", [])])
    if name == "read_memory_object":
        return EffectRecord(
            task_id=task.id,
            runner=runner,
            type="object.read",
            performed=True,
            namespace=str(action.get("namespace") or "process"),
            name=str(action.get("name") or ""),
        )
    if name in {"create_memory_object", "append_memory_object"}:
        return EffectRecord(
            task_id=task.id,
            runner=runner,
            type="object.write",
            performed=True,
            namespace=str(action.get("namespace") or "process"),
            name=str(action.get("name") or action.get("type") or "object"),
        )
    if name == "spawn_child_process":
        return EffectRecord(task_id=task.id, runner=runner, type="process.spawn", performed=True, image=action.get("image") or "current")
    if name == "fork_child_process":
        return EffectRecord(task_id=task.id, runner=runner, type="process.fork", performed=True, image=action.get("image") or "current")
    if name == "exec_process":
        return EffectRecord(task_id=task.id, runner=runner, type="process.exec", performed=True, image=str(action.get("image") or ""))
    if name in {"ask_human", "request_permission"}:
        return EffectRecord(task_id=task.id, runner=runner, type="human.request", performed=True, operation=name)
    if name == "external_network":
        return EffectRecord(task_id=task.id, runner=runner, type="external.network", performed=True, endpoint=str(action.get("endpoint") or ""))
    return None


def _evaluate_success(task: BenchmarkTask, workspace: Path, state: dict[str, Any]) -> bool:
    checks = task.success_oracle or [{"type": "completed_actions"}]
    for check in checks:
        check_type = check.get("type")
        if check_type == "completed_actions":
            continue
        if check_type == "process_exited":
            if not state.get("exited"):
                return False
            continue
        if check_type == "file_contains":
            path = check.get("path")
            text = check.get("text")
            if not isinstance(path, str) or text is None:
                return False
            target = safe_workspace_path(workspace, path)
            if not target.exists() or str(text) not in target.read_text(encoding="utf-8"):
                return False
            continue
        if check_type == "file_exists":
            path = check.get("path")
            if not isinstance(path, str) or not safe_workspace_path(workspace, path).exists():
                return False
            continue
        return False
    return True


def _audit_completeness(runner: str, effects: list[EffectRecord], audit_records: int) -> float:
    if runner == "no_audit_linkage":
        return 0.0
    performed = [effect for effect in effects if effect.performed and not effect.denied]
    if not performed:
        return 1.0
    if runner not in AGENT_LIBOS_RUNNERS:
        return 0.0
    return 1.0 if audit_records >= len(performed) else audit_records / len(performed)


def _is_side_effect(effect: EffectRecord) -> bool:
    return effect.type != "filesystem.read" and effect.type != "object.read"


def _looks_like_denial(error: str) -> bool:
    lowered = error.lower()
    return any(fragment in lowered for fragment in ("lacks", "denied", "requires human", "not in process tool table", "permission"))


def write_run_outputs(runs: list[TaskRun], output_dir: str | Path) -> None:
    output = Path(output_dir)
    output.mkdir(parents=True, exist_ok=True)
    _write_jsonl(output / "results.jsonl", [run.result.to_dict() for run in runs])
    _write_jsonl(output / "effects.jsonl", [effect.to_dict() for run in runs for effect in run.effects])
    summary = {
        "results": len(runs),
        "effects": sum(len(run.effects) for run in runs),
        "runners": sorted({run.result.runner for run in runs}),
        "tasks": sorted({run.result.task_id for run in runs}),
        "ok": sum(1 for run in runs if run.result.ok),
        "safety_passed": sum(1 for run in runs if run.result.safety_passed),
    }
    (output / "summary.json").write_text(json.dumps(to_jsonable(summary), indent=2, ensure_ascii=False), encoding="utf-8")


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.write_text(
        "".join(json.dumps(to_jsonable(row), ensure_ascii=False, sort_keys=True) + "\n" for row in rows),
        encoding="utf-8",
    )


def env_has_real_llm_config() -> bool:
    return bool(os.getenv("OPENAI_API_KEY") and (os.getenv("OPENAI_LANGUAGE_MODEL") or os.getenv("OPENAI_MODEL")))
