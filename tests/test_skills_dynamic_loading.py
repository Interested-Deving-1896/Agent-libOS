from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from typing import Any

from agent_libos import AgentImage, Runtime
from agent_libos.api.cli import main as cli_main
from agent_libos.config import AgentLibOSConfig, SkillDefaults
from agent_libos.llm.client import LLMCompletion
from agent_libos.models import CapabilityRight, ValidationResult
from agent_libos.models.exceptions import CapabilityDenied, HumanApprovalRequired, ValidationError
from agent_libos.runtime.syscalls import LibOSSyscallSession
from agent_libos.substrate import LocalResourceProviderSubstrate
from agent_libos.tools.sandbox import DenoTypescriptSandbox, SandboxBackend, SyscallHandler


class SkillDynamicLoadingTests(unittest.TestCase):
    def test_skill_manifest_validation_and_global_trust(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            global_dir = root / "global-skills"
            global_dir.mkdir()
            manifest = global_dir / "trusted.yaml"
            manifest.write_text(_echo_skill_manifest("global-skill:v0"), encoding="utf-8")
            config = AgentLibOSConfig(skills=replace(SkillDefaults(), global_dirs=(str(global_dir),)))
            runtime = Runtime.open("local", config=config)
            try:
                with self.assertRaises(CapabilityDenied):
                    runtime.skills.register_global_skill_from_path(manifest, actor="cli", require_capability=False)

                trust = runtime.skills.global_manifest_info(manifest)
                runtime.skills.trust_skill_source(
                    actor="cli",
                    source_type="global",
                    source=trust["source"],
                    manifest_sha256=trust["manifest_sha256"],
                    require_capability=False,
                )
                registered = runtime.skills.register_global_skill_from_path(manifest, actor="cli", require_capability=False)

                self.assertEqual(registered["skill_id"], "global-skill:v0")
                self.assertEqual(registered["source_type"], "global")
                with self.assertRaises(ValidationError):
                    runtime.skills.register_skill_from_yaml_text(
                        "schema_version: 1\nskill_id: bad:v0\nname: Bad\nunknown: nope\n",
                        actor="cli",
                        require_capability=False,
                    )
                with self.assertRaises(ValidationError):
                    runtime.skills.register_skill_from_yaml_text(
                        "schema_version: nope\nskill_id: bad:v0\nname: Bad\n",
                        actor="cli",
                        require_capability=False,
                    )
                with self.assertRaises(ValidationError):
                    runtime.skills.register_skill_from_yaml_text(
                        """
schema_version: 1
skill_id: bad-action:v0
name: Bad Action
actions:
  - name: bad_action
    unexpected: nope
""".lstrip(),
                        actor="cli",
                        require_capability=False,
                    )
                with self.assertRaises(ValidationError):
                    runtime.skills.register_skill_from_yaml_text(
                        """
schema_version: 1
skill_id: bad-jit:v0
name: Bad JIT
jit_tools:
  - name: bad_jit
    description: Bad nested field.
    source: export function run(args, libos) { return {}; }
    source_path: escaped.ts
""".lstrip(),
                        actor="cli",
                        require_capability=False,
                    )
            finally:
                runtime.close()

    def test_workspace_load_reads_via_filesystem_and_uses_human_once_for_skill_authority(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            manifest = Path(temp_dir) / "skill.yaml"
            manifest.write_text(_echo_skill_manifest("workspace-skill:v0"), encoding="utf-8")
            runtime = Runtime.open("local", substrate=LocalResourceProviderSubstrate(temp_dir))
            try:
                pid = runtime.process.spawn(image="base-agent:v0", goal="load workspace skill")
                runtime.filesystem.grant_path(pid, "skill.yaml", [CapabilityRight.READ], issued_by="test")

                with self.assertRaises(HumanApprovalRequired) as raised:
                    runtime.skills.load_skill_from_workspace_yaml(pid, "skill.yaml")

                runtime.human.approve(raised.exception.request_id)
                loaded = runtime.skills.load_skill_from_workspace_yaml(pid, "skill.yaml")

                self.assertEqual(loaded["skill_id"], "workspace-skill:v0")
                self.assertIn("echo", runtime.process.get(pid).tool_table)
                self.assertFalse(runtime.capability.check(pid, "skill:workspace-skill:v0", CapabilityRight.EXECUTE))
            finally:
                runtime.close()

    def test_skill_syscall_load_yaml_uses_primitive_capabilities_not_tool_table(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            manifest = Path(temp_dir) / "skill.yaml"
            manifest.write_text(_echo_skill_manifest("syscall-skill:v0"), encoding="utf-8")
            runtime = Runtime.open("local", substrate=LocalResourceProviderSubstrate(temp_dir))
            try:
                pid = runtime.process.spawn(image="base-agent:v0", goal="syscall skill")
                process = runtime.process.get(pid)
                process.tool_table.pop("load_skill_from_yaml", None)
                runtime.store.update_process(process)
                runtime.filesystem.grant_path(pid, "skill.yaml", [CapabilityRight.READ], issued_by="test")
                runtime.capability.grant(
                    pid,
                    "skill:syscall-skill:v0",
                    [CapabilityRight.WRITE, CapabilityRight.EXECUTE],
                    issued_by="test",
                )

                result = self._run(LibOSSyscallSession(runtime, pid).handle("skill.load_yaml", {"path": "skill.yaml"}))

                self.assertEqual(result["skill_id"], "syscall-skill:v0")
                self.assertIn("echo", runtime.process.get(pid).tool_table)
            finally:
                runtime.close()

    def test_loaded_existing_tool_visibility_does_not_grant_resource_authority(self) -> None:
        runtime = Runtime.open("local")
        try:
            pid = runtime.process.spawn(image="base-agent:v0", goal="load read tool")
            runtime.register_skill_from_yaml_text(
                """
schema_version: 1
skill_id: read-skill:v0
name: Read Skill
tools: [read_text_file]
required_capabilities:
  - resource: filesystem:workspace:secret.txt
    rights: [read]
""".lstrip(),
                actor="cli",
            )
            runtime.capability.grant(pid, "skill:read-skill:v0", [CapabilityRight.EXECUTE], issued_by="test")

            runtime.skills.load_skill(pid, "read-skill:v0", actor=pid)
            result = runtime.tools.call(pid, "read_text_file", {"path": "secret.txt"})

            self.assertIn("read_text_file", runtime.process.get(pid).tool_table)
            self.assertFalse(runtime.capability.check(pid, "filesystem:workspace:secret.txt", CapabilityRight.READ))
            self.assertFalse(result.ok)
            self.assertIn("lacks read", result.error or "")
        finally:
            runtime.close()

    def test_unload_skill_consumes_one_time_execute_authority(self) -> None:
        runtime = Runtime.open("local")
        try:
            pid = runtime.process.spawn(image="base-agent:v0", goal="unload skill")
            runtime.register_skill_from_yaml_text(_echo_skill_manifest("unload-skill:v0"), actor="cli")
            runtime.load_skill(pid, "unload-skill:v0")

            runtime.capability.grant_once(
                pid,
                "skill:unload-skill:v0",
                [CapabilityRight.EXECUTE],
                issued_by="test",
            )
            runtime.skills.unload_skill(pid, "unload-skill:v0", actor=pid)

            self.assertFalse(runtime.capability.check(pid, "skill:unload-skill:v0", CapabilityRight.EXECUTE))
            self.assertNotIn("echo", runtime.process.get(pid).tool_table)
        finally:
            runtime.close()

    def test_jit_skill_tool_is_process_local_and_uses_existing_deno_validation_path(self) -> None:
        runtime = Runtime.open("local")
        runtime.tools.sandbox = FakeSkillDenoSandbox()
        try:
            owner = runtime.process.spawn(image="base-agent:v0", goal="load jit skill")
            other = runtime.process.spawn(image="base-agent:v0", goal="other")
            runtime.register_skill_from_yaml_text(
                """
schema_version: 1
skill_id: jit-skill:v0
name: JIT Skill
jit_tools:
  - name: skill_count
    description: Count text characters.
    input_schema:
      type: object
    output_schema:
      type: object
    source: |
      export function run(args, libos) { /* fake:count_chars */ return {}; }
    tests: [{args: {text: abc}, expected: {count: 3}}]
""".lstrip(),
                actor="cli",
            )
            runtime.capability.grant(owner, "skill:jit-skill:v0", [CapabilityRight.EXECUTE], issued_by="test")

            loaded = runtime.skills.load_skill(owner, "jit-skill:v0", actor=owner)
            result = runtime.tools.call(owner, "skill_count", {"text": "hello"})

            self.assertIn("skill_count", loaded["jit_tool_ids"])
            self.assertTrue(result.ok, result.error)
            self.assertEqual(result.payload, {"count": 5})
            self.assertIn("skill_count", runtime.process.get(owner).tool_table)
            self.assertNotIn("skill_count", runtime.process.get(other).tool_table)
        finally:
            runtime.close()

    def test_image_default_skills_spawn_fork_spawn_child_and_exec_semantics(self) -> None:
        runtime = Runtime.open("local")
        try:
            runtime.register_skill_from_yaml_text(_echo_skill_manifest("image-skill:v0"), actor="cli")
            runtime.register_skill_from_yaml_text(
                """
schema_version: 1
skill_id: parent-extra:v0
name: Parent Extra
tools: [read_text_file]
""".lstrip(),
                actor="cli",
            )
            runtime.register_image(
                AgentImage(
                    image_id="skill-image:v0",
                    name="skill-image",
                    default_tools=["human_output"],
                    default_skills=["image-skill:v0"],
                ),
                actor="test",
            )

            root = runtime.process.spawn(image="skill-image:v0", goal="root")
            runtime.capability.grant(root, "skill:parent-extra:v0", [CapabilityRight.EXECUTE], issued_by="test")
            runtime.skills.load_skill(root, "parent-extra:v0", actor=root)
            forked = runtime.process.fork(root, "forked")
            spawned = runtime.spawn_child_process(root, "spawned", image="base-agent:v0")
            runtime.exec_process(spawned, "skill-image:v0", goal="exec")

            self.assertIn("echo", runtime.process.get(root).tool_table)
            self.assertIn("read_text_file", runtime.process.get(forked).tool_table)
            self.assertNotIn("read_text_file", runtime.process.get(spawned).tool_table)
            self.assertIn("echo", runtime.process.get(spawned).tool_table)
        finally:
            runtime.close()

    def test_checkpoint_restore_preserves_loaded_skill_records_and_tool_table(self) -> None:
        runtime = Runtime.open("local")
        try:
            pid = runtime.process.spawn(image="base-agent:v0", goal="checkpoint skill")
            runtime.register_skill_from_yaml_text(
                """
schema_version: 1
skill_id: checkpoint-skill:v0
name: Checkpoint Skill
tools: [read_text_file]
""".lstrip(),
                actor="cli",
            )
            runtime.capability.grant(pid, "skill:checkpoint-skill:v0", [CapabilityRight.EXECUTE], issued_by="test")
            runtime.skills.load_skill(pid, "checkpoint-skill:v0", actor=pid)
            checkpoint_id = runtime.checkpoint.create(pid, "skill loaded", actor=pid)

            runtime.skills.unload_skill(pid, "checkpoint-skill:v0", actor=pid)
            runtime.checkpoint.restore("cli", checkpoint_id, require_capability=False)

            self.assertIn("checkpoint-skill:v0", runtime.process.get(pid).loaded_skills)
            self.assertIn("read_text_file", runtime.process.get(pid).tool_table)
        finally:
            runtime.close()

    def test_skill_cli_outputs_stable_json(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            db_path = str(root / "runtime.sqlite")
            manifest = root / "skill.yaml"
            manifest.write_text(_echo_skill_manifest("cli-skill:v0"), encoding="utf-8")

            registered = self._cli_json(["--db", db_path, "skills", "register", str(manifest)])
            discovered = self._cli_json(["--db", db_path, "skills", "discover", "--text", "cli"])
            spawned = self._cli_json(["--db", db_path, "spawn", "--goal", "cli skill"])
            loaded = self._cli_json(["--db", db_path, "skills", "load", spawned["pid"], "cli-skill:v0"])

            self.assertEqual(registered["skill_id"], "cli-skill:v0")
            self.assertEqual(discovered[0]["skill_id"], "cli-skill:v0")
            self.assertEqual(loaded["skill_id"], "cli-skill:v0")
            self.assertIn("echo", loaded["tool_names"])

    def test_skill_cli_actor_pid_register_reads_workspace_file_through_primitive(self) -> None:
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as temp_dir:
            root = Path(temp_dir).resolve()
            db_path = str(root / "runtime.sqlite")
            manifest = root / "skill.yaml"
            manifest.write_text(_echo_skill_manifest("cli-actor-skill:v0"), encoding="utf-8")
            relative_manifest = manifest.relative_to(Path.cwd().resolve()).as_posix()
            runtime = Runtime.open(db_path)
            try:
                pid = runtime.process.spawn(image="base-agent:v0", goal="actor cli skill")
                runtime.capability.grant(
                    pid,
                    "skill:cli-actor-skill:v0",
                    [CapabilityRight.WRITE],
                    issued_by="test",
                )
            finally:
                runtime.close()

            with self.assertRaises(CapabilityDenied):
                self._cli_json(["--db", db_path, "skills", "--actor-pid", pid, "register", relative_manifest])

            runtime = Runtime.open(db_path)
            try:
                runtime.filesystem.grant_path(pid, relative_manifest, [CapabilityRight.READ], issued_by="test")
            finally:
                runtime.close()

            registered = self._cli_json(["--db", db_path, "skills", "--actor-pid", pid, "register", relative_manifest])

            self.assertEqual(registered["skill_id"], "cli-actor-skill:v0")

    def test_loaded_skill_instructions_are_materialized_into_llm_prompt_and_persisted_calls(self) -> None:
        runtime = Runtime.open("local")
        try:
            runtime.llm.client = RecordingActionClient([{"action": "process_exit", "payload": {"done": True}}])
            pid = runtime.process.spawn(image="base-agent:v0", goal="use skill prompt")
            runtime.register_skill_from_yaml_text(
                """
schema_version: 1
skill_id: prompt-skill:v0
name: Prompt Skill
instructions: Always preserve the phrase skill-instruction-token in planning context.
tools: [echo]
actions:
  - name: prompt_action
    use_cases: [prompt testing]
""".lstrip(),
                actor="cli",
            )
            runtime.capability.grant(pid, "skill:prompt-skill:v0", [CapabilityRight.EXECUTE], issued_by="test")
            runtime.skills.load_skill(pid, "prompt-skill:v0", actor=pid)

            runtime.run_next_process_once()

            self.assertIn("skill-instruction-token", runtime.llm.client.user_prompts[0])
            persisted = runtime.store.list_llm_calls(pid)
            self.assertEqual(len(persisted), 1)
            self.assertIn("skill-instruction-token", persisted[0].messages[1]["content"])
        finally:
            runtime.close()

    def _cli_json(self, argv: list[str]) -> Any:
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            cli_main(argv)
        return json.loads(stdout.getvalue())

    def _run(self, awaitable: Any) -> Any:
        import asyncio

        return asyncio.run(awaitable)


class FakeSkillDenoSandbox(SandboxBackend):
    language = "typescript"

    def __init__(self) -> None:
        self.checker = DenoTypescriptSandbox(deno_executable="deno")

    def static_check(self, source_code: str) -> ValidationResult:
        return self.checker.static_check(source_code)

    async def arun_source(
        self,
        source_code: str,
        args: dict[str, Any],
        *,
        pid: str | None = None,
        syscall_handler: SyscallHandler | None = None,
        timeout: float | None = None,
    ) -> Any:
        if "fake:count_chars" in source_code:
            return {"count": len(str(args.get("text", "")))}
        return {"ok": True}

    def run_tests(
        self,
        source_code: str,
        tests: list[dict[str, Any]],
        timeout: float | None = None,
    ) -> ValidationResult:
        validation = self.static_check(source_code)
        if not validation.ok:
            return validation
        errors: list[str] = []
        for index, test in enumerate(tests, start=1):
            result = self.run_source(source_code, test.get("args", {}))
            if "expected" in test and result != test["expected"]:
                errors.append(f"test {index} expected {test['expected']!r}, got {result!r}")
        return ValidationResult(ok=not errors, errors=errors, logs="fake skill deno tests")

    def metadata_for_source(self, source_code: str) -> dict[str, Any]:
        return {"language": "typescript", "deno_version": "fake-deno", "imports": []}


class RecordingActionClient:
    def __init__(self, actions: list[dict[str, Any]]):
        self.actions = list(actions)
        self.user_prompts: list[str] = []

    def complete_action(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> LLMCompletion:
        self.user_prompts.append(str(messages[-1]["content"]))
        action = self.actions.pop(0)
        name = str(action["action"])
        args = {key: value for key, value in action.items() if key != "action"}
        return LLMCompletion(
            content="",
            tool_calls=[{"id": "skill_prompt", "name": name, "arguments": json.dumps(args)}],
        )


def _echo_skill_manifest(skill_id: str) -> str:
    return f"""
schema_version: 1
skill_id: {skill_id}
name: Echo Skill
version: v0
description: Adds echo to the process table.
instructions: Use echo for tiny deterministic checks.
tools: [echo]
actions: []
jit_tools: []
required_capabilities: []
""".lstrip()


if __name__ == "__main__":
    unittest.main()
