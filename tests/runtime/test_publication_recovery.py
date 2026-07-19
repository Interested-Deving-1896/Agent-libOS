from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from uuid import uuid4

import pytest

from agent_libos.models import (
    AgentImage,
    CapabilityRight,
    ObjectType,
    OperationOutcome,
    OperationState,
)
from agent_libos.models.exceptions import RuntimeRecoveryRequired, ValidationError
from agent_libos.process_execution import bind_process_execution
from agent_libos.runtime.runtime import Runtime
from agent_libos.utils.ids import utc_now
from agent_libos.utils.serde import dumps, loads


def _insert_exec_publication(
    runtime: Runtime,
    *,
    pid: str,
    operation_id: str | None = None,
    state: str = "applying",
) -> str:
    before = runtime.process_exec_state.capture(pid)
    publication_id = f"publication-test-{uuid4().hex}"
    admission_token = None
    with runtime.store.transaction():
        if state == "applying":
            process = runtime.process.get(pid)
            admission_token = runtime.store.claim_host_process_exec(
                pid,
                owner_id="runtime-that-crashed:process.exec",
                expected_revision=process.revision,
                expected_state_generation=process.state_generation,
                expected_execution_generation=process.execution_generation,
            )
            assert admission_token is not None
        runtime.store.insert_runtime_publication(
            publication_id=publication_id,
            kind="process_exec",
            pid=pid,
            owner_instance_id="runtime-that-crashed",
            plan={
                "pid": pid,
                "image_id": "review-agent:v0",
                "before_snapshot": before.snapshot.to_mapping(),
                "before_tool_ids": sorted(before.tool_ids),
                "operation_id": operation_id,
                "operation_binding_version": 1 if operation_id is not None else None,
                **(
                    {
                        "admission_execution_generation": admission_token.generation,
                        "admission_execution_owner_id": admission_token.owner_id,
                        "admission_execution_lease_id": admission_token.lease_id,
                    }
                    if admission_token is not None
                    else {}
                ),
            },
        )
        if operation_id is not None:
            runtime.operations.bind_runtime_publication(
                operation_id,
                publication_id=publication_id,
                publication_kind="process_exec",
                expected_kind="runtime",
                expected_name="process.exec",
                expected_actor=pid,
                expected_pid=pid,
            )
    if state == "applying":
        assert admission_token is not None
        process = runtime.process.get(pid)
        with bind_process_execution(admission_token):
            runtime.store.patch_process(
                pid,
                {"image_id": "review-agent:v0"},
                expected_revision=process.revision,
            )
        assert runtime.store.advance_runtime_publication(
            publication_id,
            state="applying",
            phase="process_exec_applied",
            expected_states={"planning"},
        )
    elif state == "committed":
        assert runtime.store.advance_runtime_publication(
            publication_id,
            state="committed",
            phase="committed",
            expected_states={"planning"},
        )
    else:  # pragma: no cover - helper contract
        raise AssertionError(state)
    return publication_id


def test_exec_rollback_preserves_unowned_image_prefixed_capability() -> None:
    runtime = Runtime.open("local")
    try:
        pid = runtime.process.spawn(goal="preserve unrelated authority")
        before = runtime.process_exec_state.capture(pid)
        publication_id = f"publication-test-{uuid4().hex}"
        state = replace(before, capability_rollback_token=publication_id)
        unrelated = runtime.capability.issue_trusted(
            pid,
            "custom:unrelated-controller",
            [CapabilityRight.READ],
            issued_by="image:unrelated-controller",
        )

        runtime.process_exec_state.restore(state, fence_execution=False)

        persisted = runtime.store.get_capability(unrelated.cap_id)
        assert persisted is not None and persisted.active
    finally:
        runtime.close()


def test_failed_exec_publication_preserves_concurrent_unowned_capability(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = Runtime.open("local")
    try:
        runtime.register_image(
            AgentImage(
                image_id="unrelated-capability-failure:v0",
                name="unrelated-capability-failure",
                default_skills=["missing-publication-skill"],
            ),
            actor="test",
        )
        pid = runtime.process.spawn(goal="failed exec owns only tagged authority")
        runtime.capability.grant(
            pid,
            runtime.image_registry.resource_for("unrelated-capability-failure:v0"),
            [CapabilityRight.READ],
            issued_by="test",
        )
        original = runtime.image_boot._configure_skills
        created: list[str] = []

        def create_unrelated_then_fail(*args: object, **kwargs: object) -> None:
            capability = runtime.capability.issue_trusted(
                pid,
                "custom:unrelated-during-exec",
                [CapabilityRight.READ],
                issued_by="image:unrelated-controller",
            )
            created.append(capability.cap_id)
            original(*args, **kwargs)

        monkeypatch.setattr(runtime.image_boot, "_configure_skills", create_unrelated_then_fail)
        with pytest.raises(Exception, match="missing-publication-skill"):
            runtime.exec_process(pid, "unrelated-capability-failure:v0")

        assert len(created) == 1
        persisted = runtime.store.get_capability(created[0])
        assert persisted is not None and persisted.active
        publication = runtime.store.list_runtime_publications(pid=pid)[-1]
        assert publication["state"] == "rolled_back"
        assert created[0] not in {
            artifact.get("capability_id")
            for artifact in publication["receipt"]["artifacts"]
        }
    finally:
        runtime.close()


def test_exec_recovery_cas_conflict_is_not_reported_as_rolled_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = Runtime.open("local")
    try:
        pid = runtime.process.spawn(goal="force restore CAS conflict")
        publication_id = _insert_exec_publication(runtime, pid=pid)
        monkeypatch.setattr(runtime.image_boot, "_require_recovery_lease", lambda: None)
        monkeypatch.setattr(runtime.store, "restore_process_for_exec", lambda *args, **kwargs: False)

        with pytest.raises(ValidationError, match="cannot recover process exec publication"):
            runtime.image_boot.recover_incomplete_publications()

        publication = runtime.store.get_runtime_publication(publication_id)
        assert publication is not None
        assert publication["state"] == "failed"
        assert publication["phase"] == "startup_compensation_failed"
    finally:
        runtime.close()


def test_online_exec_compensation_failure_fences_until_reopen_reconciles_operation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = tmp_path / "online-compensation-failure.sqlite"
    runtime = Runtime.open(db_path)
    publication_id = ""
    operation_id = ""
    try:
        runtime.register_image(
            AgentImage(
                image_id="compensation-failure:v0",
                name="compensation-failure",
                default_skills=["missing-compensation-skill"],
            ),
            actor="test",
        )
        pid = runtime.process.spawn(goal="uncertain exec compensation")
        runtime.capability.grant(
            pid,
            runtime.image_registry.resource_for("compensation-failure:v0"),
            [CapabilityRight.READ],
            issued_by="test",
        )

        def fail_restore(*_args: object, **_kwargs: object) -> None:
            raise RuntimeError("injected compensation failure")

        monkeypatch.setattr(runtime.process_exec_state, "restore", fail_restore)
        with pytest.raises(RuntimeRecoveryRequired) as caught:
            runtime.exec_process(pid, "compensation-failure:v0")

        publication = runtime.store.list_runtime_publications(pid=pid)[-1]
        publication_id = str(publication["publication_id"])
        operation_id = str(publication["plan"]["operation_id"])
        operation = [
            record
            for record in runtime.store.list_operations(pid=pid)
            if record.name == "process.exec"
        ][-1]
        assert caught.value.publication_id == publication_id
        assert caught.value.operation_id == operation_id
        assert caught.value.pid == pid
        assert publication["state"] == "failed"
        assert publication["phase"] == "compensation_failed"
        assert operation.state == OperationState.TERMINAL
        assert operation.outcome == OperationOutcome.UNKNOWN
        assert operation.metadata["runtime_publication_id"] == publication_id
        assert runtime.lifecycle.state == "close_failed"
    finally:
        reason = runtime.lifecycle.shutdown_reason
        if (
            runtime.lifecycle.state == "close_failed"
            and isinstance(reason, str)
            and reason.startswith("runtime.recovery_required:")
        ):
            result = runtime.release_recovery_diagnostics()
            assert result["ok"] is True, result
            assert result["recovery_diagnostics_released"] is True
        else:
            runtime.close()

    reopened = Runtime.open(db_path)
    try:
        publication = reopened.store.get_runtime_publication(publication_id)
        operation = reopened.store.get_operation(operation_id)
        assert publication is not None
        assert publication["state"] == "rolled_back"
        assert publication["phase"] == "startup_compensated"
        assert operation is not None
        assert operation.state == OperationState.TERMINAL
        assert operation.outcome == OperationOutcome.FAILED
        assert operation.metadata["runtime_publication_id"] == publication_id
        assert reopened.lifecycle.state == "open"
    finally:
        reopened.close()


def test_unknown_publication_artifact_fails_closed_with_unknown_operation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = Runtime.open("local")
    try:
        pid = runtime.process.spawn(goal="unknown compensation handler")
        operation = runtime.operations.start(
            kind="runtime",
            name="process.exec",
            actor=pid,
            pid=pid,
        )
        publication_id = _insert_exec_publication(
            runtime,
            pid=pid,
            operation_id=operation.operation_id,
        )
        monkeypatch.setattr(runtime.image_boot, "_require_recovery_lease", lambda: None)
        assert runtime.store.record_runtime_publication_artifact(
            publication_id,
            {"artifact_id": "unknown:one", "kind": "unknown_effect"},
            expected_states={"applying"},
        )

        with pytest.raises(ValidationError, match="cannot recover process exec publication"):
            runtime.image_boot.recover_incomplete_publications()

        publication = runtime.store.get_runtime_publication(publication_id)
        recovered_operation = runtime.store.get_operation(operation.operation_id)
        assert publication is not None and publication["state"] == "failed"
        assert publication["phase"] == "startup_compensation_failed"
        assert recovered_operation is not None
        assert recovered_operation.state == OperationState.TERMINAL
        assert recovered_operation.outcome == OperationOutcome.UNKNOWN
    finally:
        runtime.close()


@pytest.mark.parametrize(
    ("publication_state", "preexisting_outcome", "expected_outcome"),
    [
        ("applying", None, OperationOutcome.FAILED),
        ("applying", OperationOutcome.SUCCEEDED, OperationOutcome.FAILED),
        ("committed", None, OperationOutcome.SUCCEEDED),
    ],
)
def test_reopen_reconciles_exec_publication_operation_outcome(
    tmp_path: Path,
    publication_state: str,
    preexisting_outcome: OperationOutcome | None,
    expected_outcome: OperationOutcome,
) -> None:
    target = tmp_path / "runtime.sqlite"
    runtime = Runtime.open(target)
    pid = runtime.process.spawn(goal="publication operation convergence")
    operation = runtime.operations.start(
        kind="runtime",
        name="process.exec",
        actor=pid,
        pid=pid,
    )
    publication_id = _insert_exec_publication(
        runtime,
        pid=pid,
        operation_id=operation.operation_id,
        state=publication_state,
    )
    if preexisting_outcome is not None:
        runtime.operations.finish(preexisting_outcome, operation_id=operation.operation_id)
    runtime.close()

    for _attempt in range(2):
        reopened = Runtime.open(target)
        try:
            publication = reopened.store.get_runtime_publication(publication_id)
            recovered_operation = reopened.store.get_operation(operation.operation_id)
            assert publication is not None
            assert publication["state"] == (
                "rolled_back" if publication_state == "applying" else "committed"
            )
            assert recovered_operation is not None
            assert recovered_operation.state == OperationState.TERMINAL
            assert recovered_operation.outcome == expected_outcome
            assert recovered_operation.metadata["runtime_publication_id"] == publication_id
        finally:
            reopened.close()


def test_runtime_publication_recovery_claim_is_single_winner_and_counts_attempts() -> None:
    runtime = Runtime.open("local")
    try:
        publication_id = f"publication-claim-{uuid4().hex}"
        runtime.store.insert_runtime_publication(
            publication_id=publication_id,
            kind="process_exec",
            pid="pid-claim",
            owner_instance_id="crashed-runtime",
            plan={"pid": "pid-claim"},
        )
        stale = runtime.store.get_runtime_publication(publication_id)
        assert stale is not None

        first = runtime.store.claim_runtime_publication_recovery(
            publication_id,
            claimant_instance_id="recovery-a",
            expected_owner_instance_id=stale["owner_instance_id"],
            expected_state=stale["state"],
            classification="compensate_process_exec",
        )
        second = runtime.store.claim_runtime_publication_recovery(
            publication_id,
            claimant_instance_id="recovery-b",
            expected_owner_instance_id=stale["owner_instance_id"],
            expected_state=stale["state"],
            classification="compensate_process_exec",
        )
        assert first is not None
        assert second is None
        claims = [
            phase
            for phase in first["receipt"]["phases"]
            if phase.get("phase") == "recovery_claimed"
        ]
        assert claims[-1]["attempt"] == 1
        assert claims[-1]["classification"] == "compensate_process_exec"
    finally:
        runtime.close()


def test_runtime_publication_recovery_lease_fences_stale_terminal_write() -> None:
    runtime = Runtime.open("local")
    try:
        publication_id = f"publication-lease-{uuid4().hex}"
        runtime.store.insert_runtime_publication(
            publication_id=publication_id,
            kind="process_exec",
            pid="pid-lease",
            owner_instance_id="crashed-runtime",
            plan={"pid": "pid-lease"},
        )
        stale = runtime.store.get_runtime_publication(publication_id)
        assert stale is not None
        first = runtime.store.claim_runtime_publication_recovery(
            publication_id,
            claimant_instance_id="recovery-a",
            expected_owner_instance_id=stale["owner_instance_id"],
            expected_state=stale["state"],
            classification="compensate_process_exec",
            max_attempts=3,
        )
        assert first is not None
        first_lease = first["receipt"]["recovery"]["lease_id"]
        assert runtime.store.advance_runtime_publication(
            publication_id,
            state="failed",
            phase="first_attempt_failed",
            expected_states={"rollback_pending"},
            recovery_lease_id=first_lease,
        )
        failed = runtime.store.get_runtime_publication(publication_id)
        assert failed is not None
        second = runtime.store.claim_runtime_publication_recovery(
            publication_id,
            claimant_instance_id="recovery-b",
            expected_owner_instance_id=failed["owner_instance_id"],
            expected_state=failed["state"],
            classification="compensate_process_exec",
            max_attempts=3,
        )
        assert second is not None
        second_lease = second["receipt"]["recovery"]["lease_id"]
        assert second_lease != first_lease

        assert not runtime.store.advance_runtime_publication(
            publication_id,
            state="rolled_back",
            phase="stale_writer",
            expected_states={"rollback_pending"},
            recovery_lease_id=first_lease,
        )
        assert runtime.store.advance_runtime_publication(
            publication_id,
            state="rolled_back",
            phase="current_writer",
            expected_states={"rollback_pending"},
            recovery_lease_id=second_lease,
        )
    finally:
        runtime.close()


def test_runtime_publication_recovery_attempt_limit_persists_manual_disposition() -> None:
    runtime = Runtime.open("local")
    try:
        publication_id = f"publication-manual-{uuid4().hex}"
        current = runtime.store.insert_runtime_publication(
            publication_id=publication_id,
            kind="process_exec",
            pid="pid-manual",
            owner_instance_id="crashed-runtime",
            plan={"pid": "pid-manual"},
        )
        for attempt in range(1, 4):
            claimed = runtime.store.claim_runtime_publication_recovery(
                publication_id,
                claimant_instance_id=f"recovery-{attempt}",
                expected_owner_instance_id=current["owner_instance_id"],
                expected_state=current["state"],
                classification="compensate_process_exec",
                max_attempts=2,
            )
            assert claimed is not None
            current = claimed
            if attempt < 3:
                lease_id = current["receipt"]["recovery"]["lease_id"]
                assert runtime.store.advance_runtime_publication(
                    publication_id,
                    state="failed",
                    phase=f"attempt_{attempt}_failed",
                    expected_states={"rollback_pending"},
                    recovery_lease_id=lease_id,
                )
                refreshed = runtime.store.get_runtime_publication(publication_id)
                assert refreshed is not None
                current = refreshed

        assert current["state"] == "manual"
        assert current["phase"] == "recovery_attempts_exhausted"
        assert current["receipt"]["recovery"]["attempt"] == 3
        assert current["receipt"]["recovery"]["disposition"] == "manual"
    finally:
        runtime.close()


def test_checkpoint_capability_remap_carries_exact_publication_owner() -> None:
    row = {
        "cap_id": "cap-old",
        "subject": "pid-old",
        "resource": "object_namespace:process/pid-old",
        "rights_json": dumps(["read"]),
        "constraints_json": dumps({}),
        "status": "active",
        "issued_by": "memory.process_namespace",
        "issued_at": utc_now(),
        "issuer_cap_id": None,
        "parent_cap_id": None,
        "delegation_depth": 0,
        "metadata_json": dumps({}),
    }

    from agent_libos.runtime.checkpoint_image import CheckpointImageInstaller

    remapped = CheckpointImageInstaller._remap_capability_row(
        row,
        "pid-new",
        {},
        {"process/pid-old": "process/pid-new"},
        {"cap-old": "cap-new"},
        utc_now(),
        publication_id="publication-owner",
    )

    assert loads(remapped["metadata_json"], {})["runtime_publication_id"] == "publication-owner"


def test_checkpoint_fallback_handle_is_receipted_and_exactly_compensated() -> None:
    runtime = Runtime.open("local")
    try:
        pid = runtime.process.spawn(goal="checkpoint fallback handle")
        obj = runtime.memory.create_object(pid, ObjectType.ARTIFACT, {"value": 1})
        publication_id = f"publication-checkpoint-{uuid4().hex}"
        runtime.store.insert_runtime_publication(
            publication_id=publication_id,
            kind="process_exec",
            pid=pid,
            owner_instance_id=runtime.instance_id,
            plan={"pid": pid, "image_id": "checkpoint-test:v0"},
        )
        process = runtime.process.get(pid)
        roots = runtime.checkpoint_image_installer._remap_memory_roots(
            process,
            {
                "roots": [
                    {
                        "oid": "old-object",
                        "rights": ["read"],
                        "capability_id": "missing-capability",
                    }
                ]
            },
            {
                "oid_map": {"old-object": obj.oid},
                "capability_map": {},
            },
            publication_id=publication_id,
        )
        cap_id = roots[0].capability_id
        capability = runtime.store.get_capability(cap_id)
        publication = runtime.store.get_runtime_publication(publication_id)
        assert capability is not None
        assert capability.metadata["runtime_publication_id"] == publication_id
        assert publication is not None
        assert any(
            artifact.get("capability_id") == cap_id
            for artifact in publication["receipt"]["artifacts"]
        )

        runtime.image_boot._cleanup_publication_artifacts(
            publication,
            reason="test_checkpoint_fallback_compensation",
        )
        assert runtime.store.get_capability(cap_id) is None
    finally:
        runtime.close()


def test_publication_artifacts_are_compensated_in_reverse_receipt_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = Runtime.open("local")
    try:
        publication_id = f"publication-order-{uuid4().hex}"
        runtime.store.insert_runtime_publication(
            publication_id=publication_id,
            kind="process_exec",
            pid="pid-order",
            owner_instance_id=runtime.instance_id,
            plan={"pid": "pid-order"},
        )
        for artifact_id in ("first", "second", "third"):
            assert runtime.store.record_runtime_publication_artifact(
                publication_id,
                {
                    "artifact_id": artifact_id,
                    "kind": "loaded_skill",
                    "skill_id": artifact_id,
                },
            )
        publication = runtime.store.get_runtime_publication(publication_id)
        assert publication is not None
        observed: list[str] = []

        def capture(
            _publication_id: str,
            _pid: str,
            artifact: dict[str, object],
            **_kwargs: object,
        ) -> None:
            observed.append(str(artifact["artifact_id"]))

        monkeypatch.setattr(runtime.image_boot, "_cleanup_publication_artifact", capture)
        runtime.image_boot._cleanup_publication_artifacts(publication, reason="order-test")
        assert observed == ["third", "second", "first"]
    finally:
        runtime.close()


def test_builder_recovers_publications_before_global_jit_rehydrate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "runtime.sqlite"
    runtime = Runtime.open(target)
    runtime.close()

    from agent_libos.runtime.image_boot import ImageBootService
    from agent_libos.tools.broker import ToolBroker

    observed: list[str] = []
    original_recover = ImageBootService.recover_incomplete_publications
    original_rehydrate = ToolBroker.rehydrate_registered_jit_tools

    def recover(self: ImageBootService) -> list[str]:
        observed.append("publication_recovery")
        return original_recover(self)

    def rehydrate(self: ToolBroker) -> dict[str, list[dict[str, str]]]:
        observed.append("jit_rehydrate")
        return original_rehydrate(self)

    monkeypatch.setattr(ImageBootService, "recover_incomplete_publications", recover)
    monkeypatch.setattr(ToolBroker, "rehydrate_registered_jit_tools", rehydrate)
    reopened = Runtime.open(target)
    reopened.close()

    assert observed.index("publication_recovery") < observed.index("jit_rehydrate")
