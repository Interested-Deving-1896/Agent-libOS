from __future__ import annotations

import ast
from dataclasses import dataclass
from typing import Any

from agent_libos.models.exceptions import ValidationError


def load_yaml_mapping(text: str) -> dict[str, Any]:
    """Load a YAML mapping, using PyYAML when available and a strict fallback otherwise."""

    try:
        import yaml  # type: ignore[import-not-found]
    except ModuleNotFoundError:
        data = _StrictYamlParser(text).parse()
    else:
        data = yaml.safe_load(text)
    if not isinstance(data, dict):
        raise ValidationError("YAML document must be a mapping")
    return data


@dataclass(frozen=True)
class _Line:
    raw: str
    lineno: int

    @property
    def indent(self) -> int:
        return len(self.raw) - len(self.raw.lstrip(" "))

    @property
    def stripped(self) -> str:
        return self.raw.strip()


class _StrictYamlParser:
    """Small YAML subset parser for image registration manifests.

    It intentionally supports only mappings, lists, inline scalars/lists/maps,
    and literal/folded block scalars. The runtime prefers PyYAML when installed;
    this fallback keeps image manifests usable without adding a parser dependency.
    """

    def __init__(self, text: str):
        if "\t" in text:
            raise ValidationError("YAML tabs are not supported; use spaces for indentation")
        self.lines = [_Line(line.rstrip("\n\r"), index + 1) for index, line in enumerate(text.splitlines())]

    def parse(self) -> Any:
        index = self._next_significant(0)
        if index >= len(self.lines):
            return {}
        data, index = self._parse_block(index, self.lines[index].indent)
        trailing = self._next_significant(index)
        if trailing < len(self.lines):
            line = self.lines[trailing]
            raise ValidationError(f"unexpected YAML content at line {line.lineno}")
        return data

    def _parse_block(self, index: int, indent: int) -> tuple[Any, int]:
        line = self.lines[index]
        if line.indent != indent:
            raise ValidationError(f"unexpected indentation at line {line.lineno}")
        if self._normal_text(line).startswith("- "):
            return self._parse_list(index, indent)
        return self._parse_mapping(index, indent)

    def _parse_mapping(self, index: int, indent: int) -> tuple[dict[str, Any], int]:
        result: dict[str, Any] = {}
        while True:
            index = self._next_significant(index)
            if index >= len(self.lines):
                return result, index
            line = self.lines[index]
            if line.indent < indent:
                return result, index
            if line.indent > indent:
                raise ValidationError(f"unexpected nested YAML mapping at line {line.lineno}")
            text = self._normal_text(line)
            if text.startswith("- "):
                return result, index
            key, value_text = self._split_key_value(text, line.lineno)
            if value_text in {"|", "|-", "|+", ">", ">-", ">+"}:
                result[key], index = self._collect_block_scalar(index + 1, line.indent, folded=value_text.startswith(">"))
                continue
            if value_text == "":
                next_index = self._next_significant(index + 1)
                if next_index >= len(self.lines) or self.lines[next_index].indent <= indent:
                    result[key] = {}
                    index += 1
                    continue
                result[key], index = self._parse_block(next_index, self.lines[next_index].indent)
                continue
            result[key] = self._parse_scalar(value_text)
            index += 1

    def _parse_list(self, index: int, indent: int) -> tuple[list[Any], int]:
        result: list[Any] = []
        while True:
            index = self._next_significant(index)
            if index >= len(self.lines):
                return result, index
            line = self.lines[index]
            if line.indent < indent:
                return result, index
            if line.indent > indent:
                raise ValidationError(f"unexpected nested YAML list at line {line.lineno}")
            text = self._normal_text(line)
            if not text.startswith("- "):
                return result, index
            item_text = text[2:].strip()
            if item_text == "":
                next_index = self._next_significant(index + 1)
                if next_index >= len(self.lines) or self.lines[next_index].indent <= indent:
                    result.append(None)
                    index += 1
                    continue
                item, index = self._parse_block(next_index, self.lines[next_index].indent)
                result.append(item)
                continue
            if self._looks_like_inline_mapping(item_text):
                item, index = self._parse_list_mapping_item(item_text, index + 1, indent)
                result.append(item)
                continue
            result.append(self._parse_scalar(item_text))
            index += 1

    def _parse_list_mapping_item(self, item_text: str, index: int, list_indent: int) -> tuple[dict[str, Any], int]:
        key, value_text = self._split_key_value(item_text, self.lines[index - 1].lineno)
        item: dict[str, Any] = {key: self._parse_scalar(value_text) if value_text else {}}
        next_index = self._next_significant(index)
        if next_index >= len(self.lines) or self.lines[next_index].indent <= list_indent:
            return item, index
        extra, index = self._parse_mapping(next_index, self.lines[next_index].indent)
        item.update(extra)
        return item, index

    def _collect_block_scalar(self, index: int, parent_indent: int, folded: bool) -> tuple[str, int]:
        collected: list[_Line] = []
        while index < len(self.lines):
            line = self.lines[index]
            if line.stripped and line.indent <= parent_indent:
                break
            collected.append(line)
            index += 1
        content_indent = min((line.indent for line in collected if line.stripped), default=parent_indent + 1)
        parts = [line.raw[content_indent:] if len(line.raw) >= content_indent else "" for line in collected]
        if folded:
            return "\n".join(" ".join(part.split()) for part in parts).strip() + "\n", index
        return "\n".join(parts).rstrip() + ("\n" if parts else ""), index

    def _normal_text(self, line: _Line) -> str:
        return self._strip_comment(line.raw[line.indent :]).strip()

    def _strip_comment(self, text: str) -> str:
        quote: str | None = None
        escaped = False
        for index, char in enumerate(text):
            if escaped:
                escaped = False
                continue
            if char == "\\":
                escaped = True
                continue
            if quote is not None:
                if char == quote:
                    quote = None
                continue
            if char in {'"', "'"}:
                quote = char
                continue
            if char == "#" and (index == 0 or text[index - 1].isspace()):
                return text[:index]
        return text

    def _next_significant(self, index: int) -> int:
        while index < len(self.lines):
            stripped = self.lines[index].stripped
            if stripped and not stripped.startswith("#"):
                return index
            index += 1
        return index

    def _split_key_value(self, text: str, lineno: int) -> tuple[str, str]:
        if ":" not in text:
            raise ValidationError(f"expected key/value mapping at line {lineno}")
        key, value = text.split(":", 1)
        key = key.strip()
        if not key:
            raise ValidationError(f"empty YAML key at line {lineno}")
        return key, value.strip()

    def _looks_like_inline_mapping(self, text: str) -> bool:
        if ":" not in text:
            return False
        key, rest = text.split(":", 1)
        return bool(key.strip()) and (rest == "" or rest.startswith(" "))

    def _parse_scalar(self, text: str) -> Any:
        text = text.strip()
        lowered = text.lower()
        if lowered in {"null", "none", "~"}:
            return None
        if lowered == "true":
            return True
        if lowered == "false":
            return False
        if text.startswith("[") and text.endswith("]"):
            inner = text[1:-1].strip()
            if not inner:
                return []
            return [self._parse_scalar(part) for part in self._split_inline(inner)]
        if text.startswith("{") and text.endswith("}"):
            inner = text[1:-1].strip()
            if not inner:
                return {}
            result: dict[str, Any] = {}
            for part in self._split_inline(inner):
                key, value = self._split_key_value(part, 0)
                result[key] = self._parse_scalar(value)
            return result
        if (text.startswith('"') and text.endswith('"')) or (text.startswith("'") and text.endswith("'")):
            try:
                return ast.literal_eval(text)
            except Exception as exc:  # pragma: no cover - defensive parse detail
                raise ValidationError(f"invalid quoted YAML scalar: {text}") from exc
        try:
            return int(text)
        except ValueError:
            pass
        try:
            return float(text)
        except ValueError:
            return text

    def _split_inline(self, text: str) -> list[str]:
        parts: list[str] = []
        start = 0
        quote: str | None = None
        depth = 0
        escaped = False
        for index, char in enumerate(text):
            if escaped:
                escaped = False
                continue
            if char == "\\":
                escaped = True
                continue
            if quote is not None:
                if char == quote:
                    quote = None
                continue
            if char in {'"', "'"}:
                quote = char
                continue
            if char in "[{":
                depth += 1
                continue
            if char in "]}":
                depth -= 1
                continue
            if char == "," and depth == 0:
                parts.append(text[start:index].strip())
                start = index + 1
        parts.append(text[start:].strip())
        return parts
