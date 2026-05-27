from __future__ import annotations

from typing import Any

from agent_libos.models import ToolSpec
from agent_libos.skills_tools.action_schema import ActionSchemaCompiler


def tool_spec_to_action_dict(spec: ToolSpec) -> dict[str, Any]:
    action = ActionSchemaCompiler().from_tool_spec(spec)
    return {
        "name": action.name,
        "use_cases": action.use_cases,
        "input_schema": action.input_schema,
        "output_schema": action.output_schema,
        "required_capabilities": action.required_capabilities,
        "side_effects": action.side_effects,
        "failure_modes": action.failure_modes,
        "examples": action.examples,
    }

