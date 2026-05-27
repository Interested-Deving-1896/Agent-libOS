from __future__ import annotations

from agent_libos.tools.base import BaseAgentTool
from agent_libos.models import ToolHandle
from agent_libos.tools.broker import ToolBroker


class ToolRegistry:
    def __init__(self, broker: ToolBroker):
        self.broker = broker

    def register_action(self, tool: BaseAgentTool) -> ToolHandle:
        return self.broker.register_tool(tool, registered_by="skills_tools")

    def list(self) -> list[dict[str, Any]]:
        return self.broker.list()

    def resolve(self, name_or_id: str) -> ToolHandle:
        return self.broker.resolve(name_or_id)
