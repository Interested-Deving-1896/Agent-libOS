from __future__ import annotations

from typing import Any

import yaml

from agent_libos.models.exceptions import ValidationError


def load_yaml_mapping(text: str) -> dict[str, Any]:
    """Load a YAML mapping and reject duplicate keys at every mapping level."""

    try:
        data = yaml.load(text, Loader=_UniqueKeyLoader)
    except ValidationError:
        raise
    except yaml.YAMLError as exc:
        raise ValidationError(f"invalid YAML document: {exc}") from exc
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ValidationError("YAML document must be a mapping")
    return data


class _UniqueKeyLoader(yaml.SafeLoader):
    pass


def _construct_unique_mapping(loader: yaml.SafeLoader, node: yaml.nodes.MappingNode, deep: bool = False) -> dict[Any, Any]:
    loader.flatten_mapping(node)
    mapping: dict[Any, Any] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        try:
            hash(key)
        except TypeError as exc:
            raise ValidationError(f"unhashable YAML key: {key!r}") from exc
        if key in mapping:
            raise ValidationError(f"duplicate YAML key: {key!r}")
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


_UniqueKeyLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
    _construct_unique_mapping,
)
