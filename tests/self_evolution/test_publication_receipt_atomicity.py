from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from pydantic import BaseModel

from agent_libos import Runtime
from agent_libos.models import ObjectType, ToolSpec, ValidationResult
from agent_libos.models.exceptions import NotFound, ValidationError
from agent_libos.substrate import SubprocessLimits
from agent_libos.tools.base import SyncAgentTool, ToolContext
from agent_libos.tools.sandbox import SandboxBackend
from tests.support.skills import write_skill_package


class _EmptyArgs(BaseModel):
    pass


class _PublicationTool(SyncAgentTool[_EmptyArgs]):
    name = "publication_atomic_tool"
    description = "Tool used to verify atomic publication receipts."
    args_schema = _EmptyArgs

    def run(self, args: _EmptyArgs, ctx: ToolContext) -> dict[str, bool]:
        return {"ok": True}


class _PassingSandbox(SandboxBackend):
    def static_check(self, source_code: str) -> ValidationResult:
        return ValidationResult(ok=True)

    async def arun_source(
        self,
        source_code: str,
        args: dict[str, Any],
        **kwargs: Any,
    ) -> Any:
        return dict(args)

    def run_tests(
        self,
        source_code: str,
        tests: list[dict[str, Any]],
        timeout: float | None = None,
        *,
        limits: SubprocessLimits | None = None,
        return_metrics: bool = False,
    ) -> ValidationResult:
        return ValidationResult(ok=True, metadata={})


_JIT_SOURCE = (
    "export async function run(args: unknown, libos: unknown) "
    "{ return { ok: true }; }\n"
)


def _publication(runtime: Runtime, pid: str) -> str:
    publication_id = f"publication-receipt-{uuid4().hex}"
    runtime.store.insert_runtime_publication(
        publication_id=publication_id,
        kind="process_exec",
        pid=pid,
        owner_instance_id="receipt-atomicity-test",
        plan={"pid": pid},
    )
    return publication_id


def _candidate_spec(name: str) -> dict[str, Any]:
    return {
        "name": name,
        "description": f"{name} publication test tool.",
        "input_schema": {"type": "object"},
        "output_schema": {"type": "object"},
    }


def test_candidate_proposal_and_exact_receipt_commit_atomically(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = Runtime.open("local")
    try:
        pid = runtime.process.spawn(goal="candidate publication receipt")
        publication_id = _publication(runtime, pid)

        candidate_id = runtime.tools.propose(
            pid,
            _candidate_spec("receipt_candidate"),
            _JIT_SOURCE,
            publication_id=publication_id,
        )

        publication = runtime.store.get_runtime_publication(publication_id)
        assert publication is not None
        candidate_artifact = next(
            artifact
            for artifact in publication["receipt"]["artifacts"]
            if artifact["artifact_id"] == f"candidate:{candidate_id}"
        )
        assert candidate_artifact == {
            "artifact_id": f"candidate:{candidate_id}",
            "kind": "tool_candidate",
            "candidate_id": candidate_id,
            "descriptor_state": "object",
            "descriptor_oid": candidate_artifact["descriptor_oid"],
            "pid": pid,
        }
        descriptor = runtime.store.get_object(candidate_artifact["descriptor_oid"])
        assert descriptor is not None
        assert descriptor.payload["candidate_id"] == candidate_id
        assert runtime.store.get_tool_candidate(candidate_id) is not None

        forged_publication = deepcopy(publication)
        forged_candidate = next(
            artifact
            for artifact in forged_publication["receipt"]["artifacts"]
            if artifact["artifact_id"] == f"candidate:{candidate_id}"
        )
        forged_candidate.pop("descriptor_oid")
        with pytest.raises(ValidationError, match="descriptor identity"):
            runtime.image_boot.assert_publication_artifacts_removed(
                forged_publication
            )
        forged_null = deepcopy(publication)
        forged_null_candidate = next(
            artifact
            for artifact in forged_null["receipt"]["artifacts"]
            if artifact["artifact_id"] == f"candidate:{candidate_id}"
        )
        forged_null_candidate["descriptor_state"] = "not_created"
        forged_null_candidate["descriptor_oid"] = None
        with pytest.raises(ValidationError, match="cannot omit its descriptor"):
            runtime.image_boot.assert_publication_artifacts_removed(forged_null)

        def reject_owned_object_scan(*_args: object, **_kwargs: object) -> object:
            raise AssertionError("candidate compensation scanned process objects")

        monkeypatch.setattr(
            runtime.store,
            "list_objects_owned_by",
            reject_owned_object_scan,
        )
        monkeypatch.setattr(
            runtime.uow.objects,
            "list_objects_owned_by",
            reject_owned_object_scan,
        )
        runtime.image_boot._cleanup_publication_artifacts(
            publication,
            reason="test_exact_candidate_descriptor_compensation",
        )
        assert runtime.store.get_tool_candidate(candidate_id) is None
        assert runtime.store.get_object(candidate_artifact["descriptor_oid"]) is None
        runtime.image_boot.assert_publication_artifacts_removed(publication)
    finally:
        runtime.close()


def test_missing_candidate_publication_rolls_back_candidate_and_object() -> None:
    runtime = Runtime.open("local")
    try:
        pid = runtime.process.spawn(goal="candidate receipt rejection")
        before_candidates = runtime.store.select_table_rows("tool_candidates")
        before_objects = {
            obj.oid for obj in runtime.store.list_objects() if obj.type == ObjectType.TOOL_CANDIDATE
        }

        with pytest.raises(ValidationError, match="recording tool artifact"):
            runtime.tools.propose(
                pid,
                _candidate_spec("rejected_receipt_candidate"),
                _JIT_SOURCE,
                publication_id="missing-publication",
            )

        assert runtime.store.select_table_rows("tool_candidates") == before_candidates
        assert {
            obj.oid for obj in runtime.store.list_objects() if obj.type == ObjectType.TOOL_CANDIDATE
        } == before_objects
    finally:
        runtime.close()


def test_jit_registration_and_exact_tool_receipt_commit_atomically() -> None:
    runtime = Runtime.open("local")
    runtime.tools.sandbox = _PassingSandbox()
    try:
        pid = runtime.process.spawn(goal="JIT publication receipt")
        candidate_id = runtime.tools.propose(
            pid,
            _candidate_spec("receipt_registered_jit"),
            _JIT_SOURCE,
        )
        assert runtime.tools.validate(candidate_id, pid=pid).ok

        with pytest.raises(ValidationError, match="recording tool artifact"):
            runtime.tools.register(
                pid,
                candidate_id,
                publication_id="missing-publication",
            )
        candidate = runtime.store.get_tool_candidate(candidate_id)
        assert candidate is not None and candidate.registered_tool_id is None
        assert "receipt_registered_jit" not in runtime.process.get(pid).tool_table

        publication_id = _publication(runtime, pid)
        handle = runtime.tools.register(
            pid,
            candidate_id,
            publication_id=publication_id,
        )
        publication = runtime.store.get_runtime_publication(publication_id)
        assert publication is not None
        assert {
            "artifact_id": f"tool:{handle.tool_id}",
            "kind": "tool",
            "tool_id": handle.tool_id,
            "name": handle.name,
        } in publication["receipt"]["artifacts"]
    finally:
        runtime.close()


def test_tool_install_receipt_failure_rolls_back_durable_and_loaded_state() -> None:
    runtime = Runtime.open("local")
    try:
        before_tool_ids = {str(row["tool_id"]) for row in runtime.store.list_tools()}
        with pytest.raises(ValidationError, match="recording tool artifact"):
            runtime.tools.register_tool(
                _PublicationTool(),
                publication_id="missing-publication",
            )
        assert {str(row["tool_id"]) for row in runtime.store.list_tools()} == before_tool_ids
        with pytest.raises(NotFound):
            runtime.tools.resolve(_PublicationTool.name)

        pid = runtime.process.spawn(goal="committed JIT receipt rejection")
        with pytest.raises(ValidationError, match="recording tool artifact"):
            runtime.tools.install_committed_jit(
                pid,
                name="rejected_committed_jit",
                scope="ephemeral_process",
                spec=ToolSpec(**_candidate_spec("rejected_committed_jit")),
                source_code=_JIT_SOURCE,
                registered_by="test",
                publication_id="missing-publication",
            )
        assert {str(row["tool_id"]) for row in runtime.store.list_tools()} == before_tool_ids
        assert not runtime.store.select_table_rows(
            "tool_candidates",
            "pid = ?",
            (pid,),
        )
    finally:
        runtime.close()


def test_committed_jit_records_exact_candidate_and_tool_receipts_atomically(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = Runtime.open("local")
    try:
        pid = runtime.process.spawn(goal="committed JIT exact receipts")
        publication_id = _publication(runtime, pid)

        handle = runtime.tools.install_committed_jit(
            pid,
            name="receipted_committed_jit",
            scope="ephemeral_process",
            spec=ToolSpec(**_candidate_spec("receipted_committed_jit")),
            source_code=_JIT_SOURCE,
            registered_by="test",
            publication_id=publication_id,
        )
        runtime.uow.processes.patch_process_tool_tables(
            pid,
            model_tool_table={"receipt_model_only_alias": handle.tool_id},
        )

        candidates = runtime.store.select_table_rows(
            "tool_candidates",
            "pid = ? AND registered_tool_id = ?",
            (pid, handle.tool_id),
        )
        assert len(candidates) == 1
        candidate_id = str(candidates[0]["candidate_id"])
        publication = runtime.store.get_runtime_publication(publication_id)
        assert publication is not None
        assert publication["receipt"]["artifacts"][-2:] == [
            {
                "artifact_id": f"candidate:{candidate_id}",
                "kind": "tool_candidate",
                "candidate_id": candidate_id,
                "descriptor_state": "not_created",
                "descriptor_oid": None,
                "pid": pid,
            },
            {
                "artifact_id": f"tool:{handle.tool_id}",
                "kind": "tool",
                "tool_id": handle.tool_id,
                "name": handle.name,
            },
        ]

        def reject_unbounded_lookup(*_args: object, **_kwargs: object) -> object:
            raise AssertionError("publication compensation used an unbounded lookup")

        for target, method_name in (
            (runtime.store, "list_tools"),
            (runtime.store, "list_processes"),
            (runtime.store, "list_capabilities"),
            (runtime.store, "list_objects_owned_by"),
            (runtime.uow.extensions, "list_tools"),
            (runtime.uow.processes, "list_processes"),
            (runtime.uow.authority, "list_capabilities"),
            (runtime.uow.objects, "list_objects_owned_by"),
        ):
            monkeypatch.setattr(target, method_name, reject_unbounded_lookup)

        runtime.image_boot._cleanup_publication_artifacts(
            publication,
            reason="test_exact_committed_jit_compensation",
        )
        assert runtime.store.get_tool_candidate(candidate_id) is None
        assert handle.tool_id not in runtime.uow.extensions.get_existing_tool_ids(
            (handle.tool_id,)
        )
        assert runtime.tools.loaded_tool_handle(handle.tool_id) is None
        assert runtime.tools.jit_source(handle.tool_id) is None
        process = runtime.process.get(pid)
        assert handle.tool_id not in process.tool_table.values()
        assert handle.tool_id not in process.model_tool_table.values()
        runtime.image_boot.assert_publication_artifacts_removed(publication)
    finally:
        runtime.close()


def test_skill_activation_records_exact_candidate_tool_and_loaded_skill(
    tmp_path: Path,
) -> None:
    skill_id = "receipt-jit-skill"
    skill_dir = write_skill_package(
        tmp_path,
        skill_id,
        jit_tools=[
            {
                "name": "receipt_skill_jit",
                "description": "JIT receipt test tool.",
                "source_path": "scripts/receipt.ts",
                "input_schema": {"type": "object"},
                "output_schema": {"type": "object"},
                "tests": [],
            }
        ],
        scripts={"scripts/receipt.ts": _JIT_SOURCE},
    )
    runtime = Runtime.open("local")
    runtime.tools.sandbox = _PassingSandbox()
    try:
        pid = runtime.process.spawn(goal="Skill publication receipt")
        runtime.skills.register_skill_from_path(
            skill_dir,
            actor="test",
            require_capability=False,
        )
        publication_id = _publication(runtime, pid)

        result = runtime.skills.activate_skill(
            pid,
            skill_id,
            actor="test",
            require_capability=False,
            publication_id=publication_id,
        )

        publication = runtime.store.get_runtime_publication(publication_id)
        assert publication is not None
        artifacts = publication["receipt"]["artifacts"]
        candidate_artifacts = [item for item in artifacts if item["kind"] == "tool_candidate"]
        tool_artifacts = [item for item in artifacts if item["kind"] == "tool"]
        skill_artifacts = [item for item in artifacts if item["kind"] == "loaded_skill"]
        assert len(candidate_artifacts) == len(tool_artifacts) == len(skill_artifacts) == 1
        assert candidate_artifacts[0]["pid"] == pid
        assert tool_artifacts[0]["tool_id"] == result["jit_tool_ids"]["receipt_skill_jit"]
        loaded = runtime.process.get(pid).loaded_skills[skill_id]
        assert skill_artifacts[0] == {
            "artifact_id": f"skill:{pid}:{skill_id}:{loaded['loaded_at']}",
            "kind": "loaded_skill",
            "pid": pid,
            "skill_id": skill_id,
            "loaded_at": loaded["loaded_at"],
            "package_sha256": loaded["package_sha256"],
            "jit_tool_ids": [result["jit_tool_ids"]["receipt_skill_jit"]],
        }
    finally:
        runtime.close()


def test_missing_skill_publication_rolls_back_loaded_skill(tmp_path: Path) -> None:
    skill_id = "rejected-receipt-skill"
    skill_dir = write_skill_package(tmp_path, skill_id, allowed_tools=["echo"])
    runtime = Runtime.open("local")
    try:
        pid = runtime.process.spawn(goal="Skill receipt rejection")
        runtime.skills.register_skill_from_path(
            skill_dir,
            actor="test",
            require_capability=False,
        )

        with pytest.raises(ValidationError, match="recording loaded Skill"):
            runtime.skills.activate_skill(
                pid,
                skill_id,
                actor="test",
                require_capability=False,
                publication_id="missing-publication",
            )

        process = runtime.process.get(pid)
        assert skill_id not in process.loaded_skills
        assert process.tool_table.get("echo") == process.model_tool_table.get("echo")
    finally:
        runtime.close()
