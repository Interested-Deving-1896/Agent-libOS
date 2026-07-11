from __future__ import annotations

from copy import deepcopy
from typing import Iterable

from agent_libos.models import ObjectMetadata


SENSITIVITY_ORDER = ("public", "normal", "confidential", "restricted", "secret")
TRUST_ORDER = ("untrusted", "unknown", "user_asserted", "verified", "trusted")
INTEGRITY_ORDER = ("untrusted", "unknown", "checked", "verified")


def propagate_object_labels(
    metadata: ObjectMetadata,
    parents: Iterable[ObjectMetadata],
) -> ObjectMetadata:
    """Conservatively merge metadata-only labels from source objects.

    This is deliberately not an enforcement mechanism. It makes derived
    object labels deterministic so Explain and future sink policies have a
    stable source of evidence.
    """

    selected = deepcopy(metadata)
    sources = list(parents)
    if not sources:
        return selected
    selected.sensitivity = _highest(
        [selected.sensitivity, *(item.sensitivity for item in sources)],
        SENSITIVITY_ORDER,
    )
    selected.trust_level = _lowest(
        [selected.trust_level, *(item.trust_level for item in sources)],
        TRUST_ORDER,
    )
    selected.integrity = _lowest(
        [selected.integrity, *(item.integrity for item in sources)],
        INTEGRITY_ORDER,
    )
    selected.origin = _merge_identity(selected.origin, [item.origin for item in sources], "derived")
    selected.tenant = _merge_identity(selected.tenant, [item.tenant for item in sources], "mixed")
    selected.principal = _merge_identity(selected.principal, [item.principal for item in sources], "mixed")
    selected.declassification_authority = _merge_declassification_authority(
        selected.declassification_authority,
        [item.declassification_authority for item in sources],
    )
    return selected


def labels_for_explain(metadata: ObjectMetadata) -> dict[str, str | None]:
    return {
        "sensitivity": metadata.sensitivity,
        "trust_level": metadata.trust_level,
        "integrity": metadata.integrity,
        "origin": metadata.origin,
        "tenant": metadata.tenant,
        "principal": metadata.principal,
        "declassification_authority": metadata.declassification_authority,
    }


def is_label_downgrade(current: ObjectMetadata, proposed: ObjectMetadata) -> bool:
    sensitivity = {value: index for index, value in enumerate(SENSITIVITY_ORDER)}
    trust = {value: index for index, value in enumerate(TRUST_ORDER)}
    integrity = {value: index for index, value in enumerate(INTEGRITY_ORDER)}
    return (
        sensitivity.get(proposed.sensitivity, len(sensitivity))
        < sensitivity.get(current.sensitivity, len(sensitivity))
        or trust.get(proposed.trust_level, trust.get("unknown", 0))
        > trust.get(current.trust_level, trust.get("unknown", 0))
        or integrity.get(proposed.integrity, integrity.get("unknown", 0))
        > integrity.get(current.integrity, integrity.get("unknown", 0))
    )


def _highest(values: Iterable[str], order: tuple[str, ...]) -> str:
    rankings = {value: index for index, value in enumerate(order)}
    selected = [str(value or "unknown") for value in values]
    return max(selected, key=lambda value: rankings.get(value, len(order)))


def _lowest(values: Iterable[str], order: tuple[str, ...]) -> str:
    rankings = {value: index for index, value in enumerate(order)}
    selected = [str(value or "unknown") for value in values]
    return min(selected, key=lambda value: rankings.get(value, rankings.get("unknown", 0)))


def _merge_identity(explicit: str | None, inherited: Iterable[str | None], mixed: str) -> str | None:
    if explicit not in {None, "", "local", "unknown"}:
        return explicit
    values = sorted({str(value) for value in inherited if value not in {None, ""}})
    if not values:
        return explicit
    if len(values) == 1:
        return values[0]
    return mixed


def _merge_declassification_authority(
    explicit: str | None,
    inherited: Iterable[str | None],
) -> str | None:
    values = {str(value) for value in inherited if value not in {None, ""}}
    if explicit not in {None, ""}:
        values.add(explicit)
    return next(iter(values)) if len(values) == 1 else None
