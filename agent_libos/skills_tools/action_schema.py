from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from agent_libos.models import ToolSpec


@dataclass(frozen=True)
class ActionSchema:
    name: str
    use_cases: list[str] = field(default_factory=list)
    input_schema: dict[str, Any] = field(default_factory=dict)
    output_schema: dict[str, Any] = field(default_factory=dict)
    required_capabilities: list[dict[str, Any]] = field(default_factory=list)
    side_effects: list[str] = field(default_factory=list)
    failure_modes: list[str] = field(default_factory=list)
    examples: list[dict[str, Any]] = field(default_factory=list)


class ActionSchemaCompiler:
    def from_tool_spec(self, spec: ToolSpec) -> ActionSchema:
        return ActionSchema(
            name=spec.name,
            use_cases=[spec.description] if spec.description else [],
            input_schema=spec.input_schema,
            output_schema=spec.output_schema,
            required_capabilities=spec.required_capabilities,
            side_effects=spec.side_effects,
            failure_modes=["capability_denied", "tool_failed", "human_approval_required"],
        )

