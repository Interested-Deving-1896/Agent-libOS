from __future__ import annotations

from typing import Any

from agent_libos.models import ToolHandle
from agent_libos.tools.broker import StaticHandler, ToolBroker


class ToolRegistry:
    def __init__(self, broker: ToolBroker):
        self.broker = broker

    def register_action(
        self,
        name: str,
        handler: StaticHandler,
        description: str = "",
        input_schema: dict[str, Any] | None = None,
        output_schema: dict[str, Any] | None = None,
        side_effects: list[str] | None = None,
    ) -> ToolHandle:
        return self.broker.register_static(
            name=name,
            handler=handler,
            description=description,
            input_schema=input_schema,
            output_schema=output_schema,
            side_effects=side_effects,
            registered_by="skills_tools",
        )

    def list(self) -> list[dict[str, Any]]:
        return self.broker.list()

    def resolve(self, name_or_id: str) -> ToolHandle:
        return self.broker.resolve(name_or_id)

