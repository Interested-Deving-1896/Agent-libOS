from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from typing import Any, Iterable

from agent_libos.models import DataFlowContext, ObjectMetadata


SENSITIVITY_ORDER = ("public", "normal", "confidential", "restricted", "secret")
TRUST_ORDER = ("untrusted", "unknown", "user_asserted", "verified", "trusted")
INTEGRITY_ORDER = ("untrusted", "unknown", "checked", "verified")
LABEL_FIELDS = (
    "sensitivity",
    "trust_level",
    "integrity",
    "origin",
    "tenant",
    "principal",
    "declassification_authority",
)


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
    _validate_ordered_label(selected.sensitivity, SENSITIVITY_ORDER, "sensitivity")
    _validate_ordered_label(selected.trust_level, TRUST_ORDER, "trust_level")
    _validate_ordered_label(selected.integrity, INTEGRITY_ORDER, "integrity")
    for source in sources:
        _validate_ordered_label(source.sensitivity, SENSITIVITY_ORDER, "sensitivity")
        _validate_ordered_label(source.trust_level, TRUST_ORDER, "trust_level")
        _validate_ordered_label(source.integrity, INTEGRITY_ORDER, "integrity")
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
    selected.origin = _merge_origin(selected.origin, [item.origin for item in sources])
    selected.tenant = _merge_security_identity(selected.tenant, [item.tenant for item in sources])
    selected.principal = _merge_security_identity(selected.principal, [item.principal for item in sources])
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


def metadata_from_labels(value: Any) -> ObjectMetadata | None:
    """Decode trusted internal flow-label metadata without accepting presentation fields.

    Tool arguments never call this helper directly.  It exists so runtime
    boundaries can carry an aggregate label set in ``ToolContext.metadata``
    while the source Object ids remain the preferred, independently resolved
    provenance.
    """

    if value is None:
        return None
    if isinstance(value, ObjectMetadata):
        return deepcopy(value)
    to_dict = getattr(value, "to_dict", None)
    if callable(to_dict):
        value = to_dict()
    if not isinstance(value, Mapping):
        return None
    nested = value.get("data_labels")
    if isinstance(nested, Mapping):
        value = nested
    selected = {field: value[field] for field in LABEL_FIELDS if field in value}
    return ObjectMetadata(**selected) if selected else None


def flow_context_parts(metadata: Any) -> tuple[list[str] | None, Any | None]:
    """Read the runtime-owned flow context shape used by model-facing tools.

    The canonical form is ``metadata['data_flow_context']`` containing a
    ``DataFlowContext``.  Flat ``source_oids``/``data_labels`` metadata remains
    supported for callers created before that strong type existed.
    """

    nested = metadata.get("data_flow_context") if isinstance(metadata, Mapping) else None
    context = nested if nested is not None else metadata
    if isinstance(context, Mapping):
        raw_oids = context.get("source_oids")
        raw_refs = context.get("source_refs")
        labels = context.get("data_labels", context.get("labels"))
    else:
        raw_oids = getattr(context, "source_oids", None)
        raw_refs = getattr(context, "source_refs", None)
        labels = getattr(context, "data_labels", getattr(context, "labels", None))
    if raw_oids is None and raw_refs is not None:
        raw_oids = [
            ref.get("oid") if isinstance(ref, Mapping) else getattr(ref, "oid", None)
            for ref in raw_refs
        ]
    if raw_oids is None:
        return None, labels
    if isinstance(raw_oids, (str, bytes)):
        raise ValueError("internal data-flow source_oids must be a collection")
    source_oids = [str(oid or "").strip() for oid in raw_oids]
    if any(not oid for oid in source_oids):
        raise ValueError("internal data-flow source references require Object ids")
    return source_oids, labels


def flow_context_value(metadata: Any) -> DataFlowContext | None:
    if not isinstance(metadata, Mapping):
        return None
    value = metadata.get("data_flow_context")
    if value is None:
        return None
    if isinstance(value, DataFlowContext):
        return value
    if not isinstance(value, Mapping):
        raise ValueError("internal data_flow_context must use DataFlowContext")
    try:
        return DataFlowContext(
            labels=value.get("labels", value.get("data_labels", {})),
            source_refs=tuple(value.get("source_refs") or ()),
            materialization_id=value.get("materialization_id"),
        )
    except (TypeError, ValueError) as exc:
        raise ValueError(f"invalid internal data_flow_context: {exc}") from exc


def is_label_downgrade(current: ObjectMetadata, proposed: ObjectMetadata) -> bool:
    sensitivity = {value: index for index, value in enumerate(SENSITIVITY_ORDER)}
    trust = {value: index for index, value in enumerate(TRUST_ORDER)}
    integrity = {value: index for index, value in enumerate(INTEGRITY_ORDER)}
    _validate_ordered_label(current.sensitivity, SENSITIVITY_ORDER, "sensitivity")
    _validate_ordered_label(proposed.sensitivity, SENSITIVITY_ORDER, "sensitivity")
    _validate_ordered_label(current.trust_level, TRUST_ORDER, "trust_level")
    _validate_ordered_label(proposed.trust_level, TRUST_ORDER, "trust_level")
    _validate_ordered_label(current.integrity, INTEGRITY_ORDER, "integrity")
    _validate_ordered_label(proposed.integrity, INTEGRITY_ORDER, "integrity")
    return (
        sensitivity[proposed.sensitivity] < sensitivity[current.sensitivity]
        or trust[proposed.trust_level] > trust[current.trust_level]
        or integrity[proposed.integrity] > integrity[current.integrity]
        or proposed.tenant != current.tenant
        or proposed.principal != current.principal
        or proposed.declassification_authority != current.declassification_authority
    )


def is_conservative_label_propagation(current: ObjectMetadata, proposed: ObjectMetadata) -> bool:
    """Return whether a trusted source merge only makes labels more restrictive."""

    sensitivity = {value: index for index, value in enumerate(SENSITIVITY_ORDER)}
    trust = {value: index for index, value in enumerate(TRUST_ORDER)}
    integrity = {value: index for index, value in enumerate(INTEGRITY_ORDER)}
    for value, order, label in (
        (current.sensitivity, SENSITIVITY_ORDER, "sensitivity"),
        (proposed.sensitivity, SENSITIVITY_ORDER, "sensitivity"),
        (current.trust_level, TRUST_ORDER, "trust_level"),
        (proposed.trust_level, TRUST_ORDER, "trust_level"),
        (current.integrity, INTEGRITY_ORDER, "integrity"),
        (proposed.integrity, INTEGRITY_ORDER, "integrity"),
    ):
        _validate_ordered_label(value, order, label)
    return (
        sensitivity[proposed.sensitivity] >= sensitivity[current.sensitivity]
        and trust[proposed.trust_level] <= trust[current.trust_level]
        and integrity[proposed.integrity] <= integrity[current.integrity]
        and _identity_is_conservative(current.tenant, proposed.tenant)
        and _identity_is_conservative(current.principal, proposed.principal)
        and proposed.declassification_authority
        in {current.declassification_authority, None}
    )


def _highest(values: Iterable[str], order: tuple[str, ...]) -> str:
    rankings = {value: index for index, value in enumerate(order)}
    selected = [str(value) for value in values]
    for value in selected:
        _validate_ordered_label(value, order, "label")
    return max(selected, key=rankings.__getitem__)


def _lowest(values: Iterable[str], order: tuple[str, ...]) -> str:
    rankings = {value: index for index, value in enumerate(order)}
    selected = [str(value) for value in values]
    for value in selected:
        _validate_ordered_label(value, order, "label")
    return min(selected, key=rankings.__getitem__)


def _merge_origin(explicit: str | None, inherited: Iterable[str | None]) -> str | None:
    if explicit not in {None, "", "local", "unknown"}:
        return explicit
    values = sorted({str(value) for value in inherited if value not in {None, ""}})
    if not values:
        return explicit
    if len(values) == 1:
        return values[0]
    return "derived"


def _merge_security_identity(explicit: str | None, inherited: Iterable[str | None]) -> str | None:
    """Merge tenant/principal labels without allowing an explicit override.

    Identity labels are security domains rather than descriptive metadata.  A
    derived object that combines different domains must stay visibly mixed;
    selecting either the caller-provided value or one parent would launder the
    other source domain.
    """

    values = {
        str(value)
        for value in [explicit, *inherited]
        if value not in {None, ""}
    }
    if not values:
        return None
    if "mixed" in values or len(values) > 1:
        return "mixed"
    return next(iter(values))


def _merge_declassification_authority(
    explicit: str | None,
    inherited: Iterable[str | None],
) -> str | None:
    values = {str(value) for value in inherited if value not in {None, ""}}
    if explicit not in {None, ""}:
        values.add(explicit)
    return next(iter(values)) if len(values) == 1 else None


def _identity_is_conservative(current: str | None, proposed: str | None) -> bool:
    if proposed == current:
        return True
    if proposed == "mixed":
        return True
    return current is None and proposed is not None


def _validate_ordered_label(value: str, order: tuple[str, ...], label: str) -> None:
    if not isinstance(value, str) or value not in order:
        raise ValueError(f"invalid object data label {label}: {value!r}")
