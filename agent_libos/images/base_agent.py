from __future__ import annotations

from agent_libos.models import AgentImage


DEFAULT_IMAGES: dict[str, AgentImage] = {
    "base-agent:v0": AgentImage(
        image_id="base-agent:v0",
        name="base-agent",
        version="v0",
        system_prompt="General purpose Agent libOS process image.",
        default_tools=[
            "create_memory_object",
            "get_current_time",
            "human_output",
            "process_exit",
            "request_permission",
            "sleep",
        ],
        context_policy="plan_first",
        required_capabilities=[{"resource": "human:owner", "rights": ["write"]}],
    ),
    "coding-agent:v0": AgentImage(
        image_id="coding-agent:v0",
        name="coding-agent",
        version="v0",
        system_prompt="Software engineering process image for repository inspection, patch planning, and test execution.",
        default_tools=[
            "create_memory_object",
            "create_object_from_file",
            "get_current_time",
            "human_output",
            "parse_pytest_log",
            "process_exit",
            "read_text_file",
            "request_permission",
            "sleep",
            "write_object_to_file",
            "write_text_file",
        ],
        context_policy="error_debug",
        safety_profile="coding",
        required_capabilities=[
            {"resource": "human:owner", "rights": ["write"]},
            {"resource": "filesystem:workspace:*", "rights": ["read"]},
        ],
    ),
    "toolmaker-agent:v0": AgentImage(
        image_id="toolmaker-agent:v0",
        name="toolmaker-agent",
        version="v0",
        system_prompt="Ephemeral tool generation and validation image.",
        context_policy="minimal",
        safety_profile="toolmaker",
    ),
    "review-agent:v0": AgentImage(
        image_id="review-agent:v0",
        name="review-agent",
        version="v0",
        system_prompt="Review and validation image.",
        default_tools=[
            "create_object_from_file",
            "get_current_time",
            "human_output",
            "read_text_file",
            "request_permission",
            "sleep",
            "write_object_to_file",
            "write_text_file",
        ],
        context_policy="evidence_first",
        safety_profile="review",
    ),
}
