from __future__ import annotations

import asyncio
from collections.abc import Callable
from pathlib import Path
from typing import Any

from agent_libos.capability.manager import CapabilityManager
from agent_libos.config import DEFAULT_CONFIG, AgentLibOSConfig
from agent_libos.external import ClockPrimitive, FilesystemAdapter
from agent_libos.human.manager import HumanObjectManager
from agent_libos.images import build_default_images
from agent_libos.llm.client import LLMClient
from agent_libos.llm.executor import LLMProcessExecutor
from agent_libos.memory.object_memory import ObjectMemoryManager
from agent_libos.models import AgentImage
from agent_libos.runtime.audit_manager import AuditManager
from agent_libos.runtime.checkpoint_manager import CheckpointManager
from agent_libos.runtime.event_bus import EventBus
from agent_libos.runtime.process_manager import ProcessManager
from agent_libos.runtime.scheduler import SimpleScheduler
from agent_libos.skills.linker import SkillLinker
from agent_libos.skills.registry import RuntimeSkillRegistry
from agent_libos.storage import SQLiteStore
from agent_libos.substrate import LocalResourceProviderSubstrate, ResourceProviderSubstrate
from agent_libos.tools.broker import ToolBroker
from agent_libos.tools.builtin import (
    AskHumanTool,
    AppendMemoryObjectTool,
    CreateMemoryNamespaceTool,
    CreateMemoryObjectTool,
    CreateObjectFromFileTool,
    DeleteDirectoryTool,
    DeleteFileTool,
    EchoTool,
    ForkChildProcessTool,
    GetCurrentTimeTool,
    HumanOutputTool,
    ListChildProcessesTool,
    ListMemoryNamespaceTool,
    MergeChildMemoryTool,
    ParsePytestLogTool,
    ProcessExitTool,
    ReadDirectoryTool,
    ReadMemoryObjectTool,
    ReadTextFileTool,
    RequestPermissionTool,
    SignalChildProcessTool,
    SleepTool,
    WaitChildProcessTool,
    WriteDirectoryTool,
    WriteObjectToFileTool,
    WriteTextFileTool,
)

_RUNTIME_DEFAULTS = DEFAULT_CONFIG.runtime


class Runtime:
    """Composition root for the MVP libOS runtime."""

    def __init__(
        self,
        store: SQLiteStore,
        llm_client: LLMClient | None = None,
        substrate: ResourceProviderSubstrate | None = None,
        config: AgentLibOSConfig | None = None,
    ):
        self.config = config or DEFAULT_CONFIG
        self.substrate = substrate or LocalResourceProviderSubstrate(
            Path.cwd().resolve(),
            namespace=self.config.runtime.workspace_namespace,
        )
        self.workspace_root = Path(getattr(self.substrate, "workspace_root", self.substrate.workspace_display))
        self.store = store
        self.audit = AuditManager(store)
        self.events = EventBus(store)
        self.capability = CapabilityManager(store, self.audit, self.events, config=self.config)
        self.memory = ObjectMemoryManager(store, self.capability, self.audit, self.events, config=self.config)
        self.human = HumanObjectManager(store, self.capability, self.audit, self.events, config=self.config)
        self.clock = ClockPrimitive(
            self.audit,
            self.events,
            max_sleep_seconds=self.config.tools.max_sleep_seconds,
            provider=self.substrate.clock,
        )
        self.filesystem = FilesystemAdapter(
            self.capability,
            self.audit,
            self.events,
            human=self.human,
            provider=self.substrate.filesystem,
        )
        self.tools = ToolBroker(
            store,
            self.memory,
            self.capability,
            self.human,
            self.audit,
            self.events,
            workspace_root=self.workspace_root,
            config=self.config,
        )
        self.tools.runtime = self
        self.process = ProcessManager(store, self.memory, self.capability, self.audit, self.events, config=self.config)
        self.scheduler = SimpleScheduler(store, self.audit, poll_interval_s=self.config.scheduler.poll_interval_s)
        self.checkpoint = CheckpointManager(store, self.audit, self.events)
        self.skill_registry = RuntimeSkillRegistry()
        self.skills = SkillLinker(store, self.skill_registry, self.audit)
        self.images: dict[str, AgentImage] = build_default_images(self.config)
        self.llm = LLMProcessExecutor(self, llm_client, config=self.config)
        self._register_builtin_tools()
        self.process.add_after_spawn_hook(self._configure_process_tools_and_capabilities)

    @classmethod
    def open(
        cls,
        target: str | Path = _RUNTIME_DEFAULTS.local_store_target,
        substrate: ResourceProviderSubstrate | None = None,
        config: AgentLibOSConfig | None = None,
    ) -> "Runtime":
        selected_config = config or DEFAULT_CONFIG
        if str(target) == selected_config.runtime.local_store_target:
            return cls(SQLiteStore(":memory:"), substrate=substrate, config=selected_config)
        return cls(SQLiteStore(str(target)), substrate=substrate, config=selected_config)

    def close(self) -> None:
        self.store.close()

    def run_process_once(self, pid: str) -> dict[str, Any]:
        return self.llm.run_once(pid)

    async def arun_process_once(self, pid: str) -> dict[str, Any]:
        return await self.llm.arun_once(pid)

    def run_next_process_once(self) -> Any:
        return self.scheduler.run_once(self.arun_process_once)

    async def arun_next_process_once(self) -> Any:
        return await self.scheduler.arun_once(self.arun_process_once)

    def run_until_idle(
        self,
        max_quanta: int | None = None,
        *,
        process_human_queue: bool = True,
        human: str | None = None,
        human_auto_approve: bool | None = None,
        human_auto_policy: str | None = None,
        human_auto_answer: str | None = None,
        human_input_fn: Callable[[str], str] | None = None,
    ) -> list[Any]:
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(
                self.arun_until_idle(
                    max_quanta=max_quanta,
                    process_human_queue=process_human_queue,
                    human=human,
                    human_auto_approve=human_auto_approve,
                    human_auto_policy=human_auto_policy,
                    human_auto_answer=human_auto_answer,
                    human_input_fn=human_input_fn,
                )
            )
        raise RuntimeError("Cannot call run_until_idle() inside a running event loop. Use await arun_until_idle(...).")

    async def arun_until_idle(
        self,
        max_quanta: int | None = None,
        *,
        process_human_queue: bool = True,
        human: str | None = None,
        human_auto_approve: bool | None = None,
        human_auto_policy: str | None = None,
        human_auto_answer: str | None = None,
        human_input_fn: Callable[[str], str] | None = None,
    ) -> list[Any]:
        results: list[Any] = []
        remaining = max_quanta if max_quanta is not None else self.config.runtime.run_until_idle_max_quanta
        selected_human = human or self.config.runtime.default_human
        while remaining > 0:
            # Run all currently runnable processes first. Human queue work below
            # may wake a process, so this loop intentionally alternates between
            # process execution and terminal queue draining.
            batch = await self.scheduler.arun_until_idle(self.arun_process_once, max_quanta=remaining)
            results.extend(batch)
            remaining -= len(batch)
            if not process_human_queue:
                break
            processed = await self.human.adrain_terminal_queue(
                human=selected_human,
                auto_approve=human_auto_approve,
                auto_policy=human_auto_policy,
                auto_answer=human_auto_answer,
                input_fn=human_input_fn,
            )
            if not processed:
                break
            self.audit.record(
                actor="runtime",
                action="runtime.human_queue_drained",
                target=f"human:{selected_human}",
                decision={"request_ids": [request.request_id for request in processed]},
            )
            await asyncio.sleep(0)
        return results

    def register_image(self, image: AgentImage) -> None:
        self.images[image.image_id] = image
        self.audit.record(
            actor="runtime",
            action="image.register",
            target=f"image:{image.image_id}",
            decision={"name": image.name, "version": image.version},
        )

    def get_image(self, image_id: str) -> AgentImage:
        return self.images[image_id]

    def _configure_process_tools_and_capabilities(self, pid: str, image_id: str) -> None:
        process = self.store.get_process(pid)
        image = self.images.get(image_id) or self.images[self.config.runtime.default_image_id]
        # Tool visibility is fixed from the AgentImage at process creation time.
        # External-resource authority is still enforced later by the primitives.
        tool_names = {"process_exit", "create_memory_object", *image.default_tools}
        try:
            self.tools.configure_process_tools(pid, sorted(tool_names), assigned_by=f"image:{image_id}")
        except Exception as exc:
            self.audit.record(
                actor="runtime",
                action="image.default_tool_configure_failed",
                target=f"process:{pid}",
                decision={"tools": sorted(tool_names), "error": str(exc)},
            )
        if process is not None and process.parent_pid is not None:
            self.audit.record(
                actor="runtime",
                action="image.default_capability_skipped_for_child",
                target=f"process:{pid}",
                decision={"image": image_id, "parent_pid": process.parent_pid},
            )
            return
        for spec in image.required_capabilities:
            try:
                self.capability.grant(
                    subject=pid,
                    resource=spec["resource"],
                    rights=spec.get("rights", []),
                    issued_by=f"image:{image_id}",
                    constraints=spec.get("constraints"),
                    expires_at=spec.get("expires_at"),
                    delegable=spec.get("delegable", False),
                    revocable=spec.get("revocable", True),
                )
            except Exception as exc:
                self.audit.record(
                    actor="runtime",
                    action="image.default_capability_grant_failed",
                    target=f"process:{pid}",
                    decision={"capability": spec, "error": str(exc)},
                )

    def _register_builtin_tools(self) -> None:
        self.tools.register_tool(EchoTool(), registered_by="runtime")
        self.tools.register_tool(GetCurrentTimeTool(), registered_by="runtime")
        self.tools.register_tool(SleepTool(), registered_by="runtime")
        self.tools.register_tool(ParsePytestLogTool(), registered_by="runtime")
        self.tools.register_tool(AppendMemoryObjectTool(), registered_by="runtime")
        self.tools.register_tool(CreateMemoryNamespaceTool(), registered_by="runtime")
        self.tools.register_tool(CreateMemoryObjectTool(), registered_by="runtime")
        self.tools.register_tool(CreateObjectFromFileTool(), registered_by="runtime")
        self.tools.register_tool(DeleteDirectoryTool(), registered_by="runtime")
        self.tools.register_tool(DeleteFileTool(), registered_by="runtime")
        self.tools.register_tool(ForkChildProcessTool(), registered_by="runtime")
        self.tools.register_tool(ListChildProcessesTool(), registered_by="runtime")
        self.tools.register_tool(ListMemoryNamespaceTool(), registered_by="runtime")
        self.tools.register_tool(MergeChildMemoryTool(), registered_by="runtime")
        self.tools.register_tool(ProcessExitTool(), registered_by="runtime")
        self.tools.register_tool(RequestPermissionTool(), registered_by="runtime")
        self.tools.register_tool(ReadDirectoryTool(), registered_by="runtime")
        self.tools.register_tool(ReadMemoryObjectTool(), registered_by="runtime")
        self.tools.register_tool(ReadTextFileTool(), registered_by="runtime")
        self.tools.register_tool(SignalChildProcessTool(), registered_by="runtime")
        self.tools.register_tool(WriteObjectToFileTool(), registered_by="runtime")
        self.tools.register_tool(WriteDirectoryTool(), registered_by="runtime")
        self.tools.register_tool(WriteTextFileTool(), registered_by="runtime")
        self.tools.register_tool(AskHumanTool(), registered_by="runtime")
        self.tools.register_tool(HumanOutputTool(), registered_by="runtime")
        self.tools.register_tool(WaitChildProcessTool(), registered_by="runtime")
