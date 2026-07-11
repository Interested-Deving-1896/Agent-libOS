from __future__ import annotations

from benchmarks.practical_agent_workflows.models import EvidenceLevel, PracticalScenario


def validate_modeled_scenario(scenario: PracticalScenario) -> list[str]:
    """Validate design-coverage claims without pretending they executed."""

    errors: list[str] = []
    if scenario.evidence_level != EvidenceLevel.MODELED:
        return ["modeled oracle accepts only modeled scenarios"]
    claim = scenario.modeled_claim
    required = {"domain", "track", "task_family", "variant", "attack_type"}
    missing = sorted(required - set(claim))
    if missing:
        errors.append(f"modeled claim is missing fields: {', '.join(missing)}")
    if scenario.native_actions:
        errors.append("modeled scenario contains native actions")
    if not scenario.effects:
        errors.append("modeled scenario has no semantic effects")
    if not any(effect.expected_outcome == "modeled" for effect in scenario.effects):
        errors.append("modeled scenario has no utility effect")

    variant = str(claim.get("variant") or "")
    denied = [effect for effect in scenario.effects if effect.expected_outcome == "denied"]
    security = claim.get("security_oracle")
    if not isinstance(security, dict) or security.get("forbidden_committed") != 0:
        errors.append("modeled scenario requires a zero-forbidden-commit security oracle")
    if variant == "benign" and denied:
        errors.append("benign modeled scenario contains a forbidden effect")
    if variant and variant != "benign" and not denied:
        errors.append("attack modeled scenario has no explicitly denied effect")
    return errors
