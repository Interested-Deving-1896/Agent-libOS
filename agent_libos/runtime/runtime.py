from __future__ import annotations

from pathlib import Path
from typing import Any

from agent_libos.capability.manager import CapabilityManager
from agent_libos.external import FilesystemAdapter
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
    CreateMemoryObjectTool,
    CreateObjectFromFileTool,
    EchoTool,
    HumanOutputTool,
    ParsePytestLogTool,
    ProcessExitTool,
    ReadTextFileTool,
    RequestPermissionTool,
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

    def run_next_process_once(self) -> Any:
        return self.scheduler.run_once(self.run_process_once)

    def run_until_idle(self, max_quanta: int = 25) -> list[Any]:
        results: list[Any] = []
        for _ in range(max_quanta):
            result = self.run_next_process_once()
            if result is None:
                break
            results.append(result)
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
        self.tools.register_tool(ParsePytestLogTool(), registered_by="runtime")
        self.tools.register_tool(CreateMemoryObjectTool(), registered_by="runtime")
        self.tools.register_tool(CreateObjectFromFileTool(), registered_by="runtime")
        self.tools.register_tool(ProcessExitTool(), registered_by="runtime")
        self.tools.register_tool(RequestPermissionTool(), registered_by="runtime")
        self.tools.register_tool(ReadTextFileTool(), registered_by="runtime")
        self.tools.register_tool(WriteObjectToFileTool(), registered_by="runtime")
        self.tools.register_tool(WriteTextFileTool(), registered_by="runtime")
        self.tools.register_tool(HumanOutputTool(), registered_by="runtime")
