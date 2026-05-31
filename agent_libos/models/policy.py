from __future__ import annotations

from agent_libos.models.base import StrEnum


class PolicyDecision(StrEnum):
    ALLOW = "allow"
    DENY = "deny"
    REQUIRE_HUMAN_APPROVAL = "require_human_approval"
    REQUIRE_SANDBOX = "require_sandbox"
    REQUIRE_CHECKPOINT = "require_checkpoint"
    REQUIRE_CAPABILITY_ATTENUATION = "require_capability_attenuation"
