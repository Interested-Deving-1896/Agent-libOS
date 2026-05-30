from __future__ import annotations

import asyncio
from collections.abc import Callable
from pathlib import Path
from typing import Any

from agent_libos.capability.manager import CapabilityManager
from agent_libos.external import ClockPrimitive, FilesystemAdapter
from agent_libos.human.manager import HumanObjectManager
from agent_libos.images import DEFAULT_IMAGES
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
from agent_libos.tools.broker import ToolBroker
from agent_libos.tools.builtin import (
    AskHumanTool,
    CreateMemoryObjectTool,
    CreateObjectFromFileTool,
    EchoTool,
    GetCurrentTimeTool,
    HumanOutputTool,
    ParsePytestLogTool,
    ProcessExitTool,
    ReadTextFileTool,
    RequestPermissionTool,
    SleepTool,
    WriteObjectToFileTool,
    WriteTextFileTool,
)


class Runtime:
    def __init__(self, store: SQLiteStore, llm_client: LLMClient | None = None):
        self.workspace_root = Path.cwd().resolve()
        self.store = store
        self.audit = AuditManager(store)
        self.events = EventBus(store)
        self.capability = CapabilityManager(store, self.audit, self.events)
        self.memory = ObjectMemoryManager(store, self.capability, self.audit, self.events)
        self.human = HumanObjectManager(store, self.capability, self.audit, self.events)
        self.clock = ClockPrimitive(self.audit, self.events)
        self.filesystem = FilesystemAdapter(
            self.capability,
            self.audit,
            self.events,
            root=self.workspace_root,
            human=self.human,
        )
        self.tools = ToolBroker(
            store,
            self.memory,
            self.capability,
            self.human,
            self.audit,
            self.events,
            workspace_root=self.workspace_root,
        )
        self.tools.runtime = self
        self.process = ProcessManager(store, self.memory, self.capability, self.audit, self.events)
        self.scheduler = SimpleScheduler(store, self.audit)
        self.checkpoint = CheckpointManager(store, self.audit, self.events)
        self.skill_registry = RuntimeSkillRegistry()
        self.skills = SkillLinker(store, self.skill_registry, self.audit)
        self.images: dict[str, AgentImage] = dict(DEFAULT_IMAGES)
        self.llm = LLMProcessExecutor(self, llm_client)
        self._register_builtin_tools()
        self.process.add_after_spawn_hook(self._configure_process_tools_and_capabilities)

    @classmethod
    def open(cls, target: str | Path = "local") -> "Runtime":
        if str(target) == "local":
            return cls(SQLiteStore(":memory:"))
        return cls(SQLiteStore(str(target)))

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
        max_quanta: int = 25,
        *,
        process_human_queue: bool = True,
        human: str = "owner",
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
        max_quanta: int = 4096,
        *,
        process_human_queue: bool = True,
        human: str = "owner",
        human_auto_approve: bool | None = None,
        human_auto_policy: str | None = None,
        human_auto_answer: str | None = None,
        human_input_fn: Callable[[str], str] | None = None,
    ) -> list[Any]:
        results: list[Any] = []
        remaining = max_quanta
        while remaining > 0:
            batch = await self.scheduler.arun_until_idle(self.arun_process_once, max_quanta=remaining)
            results.extend(batch)
            remaining -= len(batch)
            if not process_human_queue:
                break
            processed = await self.human.adrain_terminal_queue(
                human=human,
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
                target=f"human:{human}",
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
        image = self.images.get(image_id) or self.images["base-agent:v0"]
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
        self.tools.register_tool(CreateMemoryObjectTool(), registered_by="runtime")
        self.tools.register_tool(CreateObjectFromFileTool(), registered_by="runtime")
        self.tools.register_tool(ProcessExitTool(), registered_by="runtime")
        self.tools.register_tool(RequestPermissionTool(), registered_by="runtime")
        self.tools.register_tool(ReadTextFileTool(), registered_by="runtime")
        self.tools.register_tool(WriteObjectToFileTool(), registered_by="runtime")
        self.tools.register_tool(WriteTextFileTool(), registered_by="runtime")
        self.tools.register_tool(AskHumanTool(), registered_by="runtime")
        self.tools.register_tool(HumanOutputTool(), registered_by="runtime")
