from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any


class EvidenceLevel(str, Enum):
    NATIVE_LIVE = "native-live"
    MODELED = "modeled"


@dataclass(frozen=True)
class SemanticEffect:
    effect_class: str
    target: str
    expected_outcome: str = "committed"


@dataclass(frozen=True)
class NativeToolAction:
    tool: str
    arguments: dict[str, Any]
    oracle_collection: str
    oracle_key: str


@dataclass(frozen=True)
class PracticalScenario:
    scenario_id: str
    title: str
    evidence_level: EvidenceLevel
    effects: tuple[SemanticEffect, ...]
    native_actions: tuple[NativeToolAction, ...] = ()
    modeled_claim: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.evidence_level == EvidenceLevel.NATIVE_LIVE and not self.native_actions:
            raise ValueError(f"native-live scenario {self.scenario_id} requires runtime tool actions")
        if self.evidence_level == EvidenceLevel.MODELED and self.native_actions:
            raise ValueError(f"modeled scenario {self.scenario_id} cannot contain native tool actions")
        if len(self.effects) != len(self.native_actions) and self.evidence_level == EvidenceLevel.NATIVE_LIVE:
            raise ValueError(f"native-live scenario {self.scenario_id} must map every effect to one tool action")


@dataclass
class PracticalScenarioResult:
    scenario_id: str
    evidence_level: EvidenceLevel
    ok: bool
    semantic_effects: int
    tool_calls: int
    operations: int
    external_effect_ids: list[str] = field(default_factory=list)
    operation_ids: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["evidence_level"] = self.evidence_level.value
        return payload


@dataclass
class PracticalRunReport:
    schema_version: int
    results: list[PracticalScenarioResult]
    scenario_counts: dict[str, int]
    semantic_effect_counts: dict[str, int]
    native_tool_calls: int
    native_operations: int
    modeled_fallback: int
    native_live_ok: bool
    modeled_suite_ok: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "results": [item.to_dict() for item in self.results],
            "scenario_counts": self.scenario_counts,
            "semantic_effect_counts": self.semantic_effect_counts,
            "native_tool_calls": self.native_tool_calls,
            "native_operations": self.native_operations,
            "modeled_fallback": self.modeled_fallback,
            "native_live_ok": self.native_live_ok,
            "modeled_suite_ok": self.modeled_suite_ok,
        }
