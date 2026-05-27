from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from agent_libos.capability.manager import CapabilityManager
from agent_libos.human.manager import HumanObjectManager
from agent_libos.images import DEFAULT_IMAGES
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


class Runtime:
    def __init__(self, store: SQLiteStore):
        self.store = store
        self.audit = AuditManager(store)
        self.events = EventBus(store)
        self.capability = CapabilityManager(store, self.audit, self.events)
        self.memory = ObjectMemoryManager(store, self.capability, self.audit, self.events)
        self.human = HumanObjectManager(store, self.capability, self.audit, self.events)
        self.tools = ToolBroker(store, self.memory, self.capability, self.human, self.audit, self.events)
        self.process = ProcessManager(store, self.memory, self.capability, self.audit, self.events)
        self.scheduler = SimpleScheduler(store, self.audit)
        self.checkpoint = CheckpointManager(store, self.audit, self.events)
        self.skill_registry = RuntimeSkillRegistry()
        self.skills = SkillLinker(store, self.skill_registry, self.audit)
        self.images: dict[str, AgentImage] = dict(DEFAULT_IMAGES)
        self._register_builtin_tools()

    @classmethod
    def open(cls, target: str | Path = "local") -> "Runtime":
        if str(target) == "local":
            return cls(SQLiteStore(":memory:"))
        return cls(SQLiteStore(str(target)))

    def close(self) -> None:
        self.store.close()

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

    def _register_builtin_tools(self) -> None:
        self.tools.register_static(
            name="echo",
            handler=lambda args: args,
            description="Return arguments unchanged.",
        )
        self.tools.register_static(
            name="parse_pytest_log",
            handler=self._parse_pytest_log,
            description="Parse pytest output into a small structured failure summary.",
            input_schema={"type": "object", "properties": {"log": {"type": "string"}}},
            output_schema={"type": "object"},
        )

    def _parse_pytest_log(self, args: dict[str, Any]) -> dict[str, Any]:
        log = args.get("log", "")
        failed: list[str] = []
        errors: list[str] = []
        assertions: list[str] = []
        for line in log.splitlines():
            stripped = line.strip()
            if stripped.startswith("FAILED "):
                failed.append(stripped)
            elif re.match(r"^E\s+", stripped):
                errors.append(stripped[2:])
            elif "AssertionError" in stripped:
                assertions.append(stripped)
        return {
            "failed": failed,
            "errors": errors,
            "assertions": assertions,
            "failure_count": len(failed) or len(assertions) or len(errors),
        }
