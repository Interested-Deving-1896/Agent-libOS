from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from agent_libos.models import (
    AgentImage,
    DataFlowContext,
    DataLabels,
    ObjectMetadata,
    ProcessStatus,
)
from agent_libos.models.exceptions import ValidationError
from agent_libos.runtime.snapshots import ExecRollbackState, SnapshotCodec
from agent_libos.utils.ids import new_id, utc_now


class ImageBootService:
    """Own image preflight, process initialization, exec, and compensation."""

    def __init__(
        self,
        *,
        process: Any,
        launch: Any,
        processes: Any,
        audit: Any,
        checkpoint: Any,
        authority_manifests: Any,
        modules: Any,
        tools: Any,
        skills: Any,
        exec_state: Any,
        checkpoint_installer: Any,
        package_installer: Any,
        store: Any,
        owner_instance_id: str,
    ) -> None:
        self._process = process
        self._launch = launch
        self._processes = processes
        self._audit = audit
        self._checkpoint = checkpoint
        self._authority_manifests = authority_manifests
        self._modules = modules
        self._tools = tools
        self._skills = skills
        self._exec_state = exec_state
        self._checkpoint_installer = checkpoint_installer
        self._package_installer = package_installer
        self._store = store
        self._owner_instance_id = owner_instance_id

    def exec(
        self,
        pid: str,
        image_id: str,
        *,
        args: dict[str, Any] | None = None,
        goal: dict[str, Any] | str | None = None,
        preserve_memory: bool = True,
        preserve_capabilities: bool = False,
        llm_profile_id: str | None = None,
        source_oids: Iterable[str] | None = None,
        source_labels: ObjectMetadata | DataLabels | dict[str, Any] | None = None,
        source_context: DataFlowContext | None = None,
    ) -> Any:
        process = self._process.get(pid)
        image = self._launch.require_image(image_id)
        if image_id != process.image_id:
            self._launch.require_image_boot_authority(pid, image_id)
        self.preflight(image)
        previous_state = self._exec_state.capture(pid)
        publication_id = new_id("publication")
        self._store.insert_runtime_publication(
            publication_id=publication_id,
            kind="process_exec",
            pid=pid,
            owner_instance_id=self._owner_instance_id,
            plan={
                "pid": pid,
                "image_id": image_id,
                "artifact_owner": f"publication:{publication_id}",
                "before_snapshot": previous_state.snapshot.to_mapping(),
                "before_tool_ids": sorted(previous_state.tool_ids),
            },
        )
        boot_kind = str(image.boot.get("kind", "fresh"))
        try:
            self._process.exec(
                pid,
                image_id,
                args=args,
                goal=goal,
                preserve_memory=preserve_memory,
                preserve_capabilities=preserve_capabilities,
                llm_profile_id=llm_profile_id,
                source_oids=source_oids,
                source_labels=source_labels,
                source_context=source_context,
                _record_evidence=False,
            )
            self._advance_publication(publication_id, "process_exec_applied")
            assigned_by = f"publication:{publication_id}"
            self._configure_tools(pid, image, assigned_by)
            self._advance_publication(publication_id, "tools_configured")
            self._instantiate_boot(pid, image, boot_kind)
            self._advance_publication(publication_id, "boot_instantiated")
            self._configure_skills(pid, image, assigned_by)
            self._advance_publication(publication_id, "skills_configured")
            with self._store.transaction():
                current = self._process.get(pid)
                event, audit = self._process.record_exec_evidence(
                    pid,
                    old_image=process.image_id,
                    args=args,
                    preserve_memory=preserve_memory,
                    preserve_capabilities=preserve_capabilities,
                    new_goal_oid=current.goal_oid if goal is not None else None,
                )
                if not self._store.advance_runtime_publication(
                    publication_id,
                    state="committed",
                    phase="committed",
                    receipt={
                        "phase": "committed",
                        "pid": pid,
                        "revision": current.revision,
                        "event_id": event.event_id,
                        "audit_id": audit.record_id,
                    },
                    expected_states={"applying"},
                ):
                    raise ValidationError(
                        f"cannot commit process exec publication: {publication_id}"
                    )
        except Exception as exc:
            self._store.advance_runtime_publication(
                publication_id,
                state="rollback_pending",
                phase="compensating",
                error={"code": "process_exec_failed", "error_type": type(exc).__name__},
                expected_states={"planning", "applying"},
            )
            cleanup_errors: list[Exception] = []
            if boot_kind == "image_package":
                try:
                    self._package_installer.cleanup(
                        pid,
                        image,
                        reason="image_package_exec_failed",
                    )
                except Exception as cleanup_error:
                    cleanup_errors.append(cleanup_error)
            try:
                self._exec_state.restore(previous_state, fence_execution=False)
            except Exception as restore_error:
                cleanup_errors.append(restore_error)
            if cleanup_errors:
                self._store.advance_runtime_publication(
                    publication_id,
                    state="failed",
                    phase="compensation_failed",
                    error={
                        "code": "process_exec_compensation_failed",
                        "error_type": type(cleanup_errors[-1]).__name__,
                    },
                    expected_states={"rollback_pending"},
                )
                raise ExceptionGroup(
                    "process exec and compensation failed",
                    [exc, *cleanup_errors],
                ) from exc
            self._store.advance_runtime_publication(
                publication_id,
                state="rolled_back",
                phase="compensated",
                receipt={"phase": "compensated", "pid": pid},
                expected_states={"rollback_pending"},
            )
            self._audit.record(
                actor="runtime",
                action="image.boot.failed",
                target=f"process:{pid}",
                decision={
                    "image": image_id,
                    "phase": "process.exec",
                    "error": str(exc),
                    "rolled_back": True,
                },
            )
            raise
        return self._process.get(pid)

    def recover_incomplete_publications(self) -> list[str]:
        recovered: list[str] = []
        for publication in self._store.list_runtime_publications(
            states={"planning", "applying", "rollback_pending"}
        ):
            if publication["kind"] != "process_exec":
                continue
            plan = publication["plan"]
            snapshot = SnapshotCodec.decode_mapping(dict(plan["before_snapshot"]))
            state = ExecRollbackState(
                snapshot=snapshot,
                tool_ids=frozenset(str(item) for item in plan.get("before_tool_ids", [])),
                tool_handles={},
            )
            self._store.advance_runtime_publication(
                publication["publication_id"],
                state="rollback_pending",
                phase="startup_compensation",
                expected_states={"planning", "applying", "rollback_pending"},
            )
            try:
                self._exec_state.restore(state)
            except Exception as exc:
                self._store.advance_runtime_publication(
                    publication["publication_id"],
                    state="failed",
                    phase="startup_compensation_failed",
                    error={"code": "process_exec_compensation_failed", "error_type": type(exc).__name__},
                    expected_states={"rollback_pending"},
                )
                raise ValidationError(
                    f"cannot recover process exec publication: {publication['publication_id']}"
                ) from exc
            self._store.advance_runtime_publication(
                publication["publication_id"],
                state="rolled_back",
                phase="startup_compensated",
                receipt={"phase": "startup_compensated", "pid": publication["pid"]},
                expected_states={"rollback_pending"},
            )
            recovered.append(publication["publication_id"])
        return recovered

    def _advance_publication(self, publication_id: str, phase: str) -> None:
        if not self._store.advance_runtime_publication(
            publication_id,
            state="applying",
            phase=phase,
            receipt={"phase": phase},
            expected_states={"planning", "applying"},
        ):
            raise ValidationError(f"process exec publication changed during {phase}: {publication_id}")

    def preflight_id(self, image_id: str) -> None:
        self.preflight(self._launch.require_image(image_id))

    def preflight(self, image: AgentImage) -> None:
        self._require_image_modules(image)
        boot_kind = str(image.boot.get("kind", "fresh"))
        if boot_kind == "checkpoint_commit":
            self._checkpoint_installer.preflight(image)
        elif boot_kind == "image_package":
            self._package_installer.preflight(image)

    def configure_spawn(self, pid: str, image_id: str) -> None:
        try:
            image = self._launch.require_image(image_id)
            boot_kind = str(image.boot.get("kind", "fresh"))
            self.preflight(image)
            assigned_by = f"image:{image_id}"
            self._configure_tools(pid, image, assigned_by)
            self._instantiate_boot(pid, image, boot_kind)
            process = self._processes.get_process(pid)
        except Exception as exc:
            self._fail_boot(pid, image_id, exc, phase="process.spawn")
            raise
        try:
            self._configure_skills(pid, image, assigned_by)
        except Exception as exc:
            if boot_kind == "image_package":
                self._package_installer.cleanup(
                    pid,
                    image,
                    reason="image_package_default_skills_failed",
                )
            self._fail_boot(pid, image_id, exc, phase="image.default_skills")
            raise
        self._record_spawn_authority(pid, image, process, boot_kind)

    def _configure_tools(
        self,
        pid: str,
        image: AgentImage,
        assigned_by: str,
    ) -> dict[str, str]:
        full_table = self._tools.configure_process_tools(
            pid,
            sorted(image.default_tools),
            assigned_by=assigned_by,
        )
        self._tools.configure_model_tool_projection(
            pid,
            sorted(self._tools.initial_tool_projection(image)),
            assigned_by=f"{assigned_by}:model_projection",
        )
        return full_table

    def _configure_skills(
        self,
        pid: str,
        image: AgentImage,
        assigned_by: str,
    ) -> None:
        process = self._processes.get_process(pid)
        if process is None:
            return
        self._apply_loaded_skill_tool_table(process)
        for skill_id in image.default_skills:
            current = self._processes.get_process(pid)
            if current is not None and skill_id in current.loaded_skills:
                continue
            self._skills.activate_skill(
                pid,
                skill_id,
                actor=assigned_by,
                require_capability=False,
            )

    def _apply_loaded_skill_tool_table(self, process: Any) -> None:
        if not process.loaded_skills:
            return
        updated = dict(process.tool_table)
        updated_model = dict(process.model_tool_table)
        for loaded in process.loaded_skills.values():
            if not isinstance(loaded, dict):
                continue
            for mapping_key in ("tool_ids", "jit_tool_ids"):
                mapping = loaded.get(mapping_key)
                if not isinstance(mapping, dict):
                    continue
                for name, tool_id in mapping.items():
                    if isinstance(name, str) and isinstance(tool_id, str):
                        updated[name] = tool_id
                        updated_model[name] = tool_id
        process.tool_table = updated
        process.model_tool_table = updated_model
        process.updated_at = utc_now()
        self._processes.patch_process(
            process.pid,
            {
                "tool_table": process.tool_table,
                "model_tool_table": process.model_tool_table,
                "updated_at": process.updated_at,
            },
            expected_revision=process.revision,
        )

    def _instantiate_boot(
        self,
        pid: str,
        image: AgentImage,
        boot_kind: str,
    ) -> None:
        if boot_kind == "checkpoint_commit":
            self._checkpoint_installer.install(pid, image)
        elif boot_kind == "image_package":
            self._package_installer.install(pid, image)

    def _require_image_modules(self, image: AgentImage) -> None:
        missing: list[dict[str, str]] = []
        for module in image.required_modules:
            module_id = str(module.get("module_id", ""))
            source_sha256 = str(module.get("source_sha256", ""))
            if module_id and not self._modules.is_loaded(
                module_id,
                source_sha256 or None,
            ):
                missing.append(
                    {
                        "module_id": module_id,
                        "source_sha256": source_sha256,
                    }
                )
        if missing:
            raise ValidationError(
                f"image requires startup modules that are not loaded: {missing}"
            )

    def _fail_boot(
        self,
        pid: str,
        image_id: str,
        exc: Exception,
        *,
        phase: str,
    ) -> None:
        process = self._processes.get_process(pid)
        if process is not None:
            process.status = ProcessStatus.FAILED
            process.status_message = str(exc)
            process.updated_at = utc_now()
            self._processes.transition_process(
                pid,
                ProcessStatus.FAILED,
                expected_revision=process.revision,
                status_message=process.status_message,
            )
        self._audit.record(
            actor="runtime",
            action="image.boot.failed",
            target=f"process:{pid}",
            decision={"image": image_id, "phase": phase, "error": str(exc)},
        )

    def _record_spawn_authority(
        self,
        pid: str,
        image: AgentImage,
        process: Any,
        boot_kind: str,
    ) -> None:
        if process is not None:
            self._checkpoint.grant_process_defaults(
                pid,
                issued_by=f"image:{image.image_id}",
            )
        if process is not None and process.parent_pid is not None:
            self._audit.record(
                actor="runtime",
                action="image.default_capability_skipped_for_child",
                target=f"process:{pid}",
                decision={
                    "image": image.image_id,
                    "parent_pid": process.parent_pid,
                },
            )
            return
        manifest = self._authority_manifests.summary_for_process(pid)
        self._audit.record(
            actor="runtime",
            action="image.required_capabilities_declared_only",
            target=f"process:{pid}",
            decision={
                "image": image.image_id,
                "boot_kind": boot_kind,
                "required_capabilities": len(image.required_capabilities),
                "authority_manifest_id": (
                    manifest.get("manifest_id") if manifest else None
                ),
                "missing_required_capabilities": (
                    manifest.get("missing_required_capabilities", [])
                    if manifest
                    else list(image.required_capabilities)
                ),
                "reason": (
                    "image requirements are declarations; launch manifests own grants"
                ),
            },
        )


__all__ = ["ImageBootService"]
