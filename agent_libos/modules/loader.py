from __future__ import annotations

import hashlib
import errno
import importlib
import importlib.abc
import importlib.machinery
import importlib.util
import json
import os
import re
import stat
import sys
import threading
from pathlib import Path
from types import ModuleType
from typing import Any

from agent_libos.config import DEFAULT_CONFIG, AgentLibOSConfig
from agent_libos.models.exceptions import CapabilityDenied, NotFound, ValidationError
from agent_libos.modules.schema import ModuleManifest, ModuleProvides, ModuleSource, ModuleSourceFile
from agent_libos.utils.ids import new_id
from agent_libos.utils.yaml_loader import load_yaml_mapping

_MODULE_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:/@+-]*$")
_PYTHON_OBJECT_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_PYTHON_MODULE_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*$")
_SYSCALL_PATTERN = re.compile(r"^[A-Za-z][A-Za-z0-9_.:-]*$")
_HEX_SHA256_PATTERN = re.compile(r"^[a-fA-F0-9]{64}$")
_WINDOWS_FORBIDDEN_PATH_CHARS = set('<>:"|?*')
_WINDOWS_RESERVED_PATH_NAMES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{index}" for index in range(1, 10)),
    *(f"LPT{index}" for index in range(1, 10)),
}
_CACHE_PACKAGE_SEGMENTS = {
    ".git",
    ".hg",
    ".svn",
    "__pycache__",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".tox",
    ".nox",
    ".venv",
    "venv",
    "node_modules",
}
_SENSITIVE_PACKAGE_FILENAMES = {
    ".env",
    ".netrc",
    "id_rsa",
    "id_dsa",
    "id_ecdsa",
    "id_ed25519",
}
_SENSITIVE_PACKAGE_SUFFIXES = (".pem", ".key", ".p12", ".pfx")
_IMPORT_LOCK = threading.RLock()
_IMPORT_CLEANUP_ATTR = "__agent_libos_package_cleanup__"


class _FreshSourceLoader(importlib.machinery.SourceFileLoader):
    def get_code(self, fullname: str) -> Any:
        source_bytes = self.get_data(self.path)
        return self.source_to_code(source_bytes, self.path)


class _SnapshotPackageImporter(importlib.abc.MetaPathFinder, importlib.abc.Loader):
    def __init__(self, package_name: str, records: tuple[ModuleSourceFile, ...], entry_module_path: str):
        self.package_name = package_name
        self.entry_module_path = entry_module_path
        self._modules: dict[str, ModuleSourceFile] = {}
        self._packages: set[str] = {package_name}
        for record in records:
            module_name = self._module_name_for_record(record)
            if module_name is not None:
                self._modules[module_name] = record
            self._add_package_dirs(record.module_path)

    def find_spec(self, fullname: str, path: Any = None, target: Any = None) -> importlib.machinery.ModuleSpec | None:
        if fullname in self._modules:
            record = self._modules[fullname]
            is_package = fullname == self.package_name and record.module_path == "__init__.py"
            return importlib.util.spec_from_loader(fullname, self, origin=record.absolute_path, is_package=is_package)
        if fullname in self._packages:
            spec = importlib.util.spec_from_loader(fullname, self, origin="<agent-libos-module-snapshot>", is_package=True)
            if spec is not None:
                spec.submodule_search_locations = []
            return spec
        return None

    def create_module(self, spec: importlib.machinery.ModuleSpec) -> ModuleType | None:
        return None

    def exec_module(self, module: ModuleType) -> None:
        record = self._modules.get(module.__name__)
        if record is None:
            module.__path__ = []  # type: ignore[attr-defined]
            module.__package__ = module.__name__
            return
        module.__file__ = record.absolute_path
        if module.__name__ == self.package_name and record.module_path == "__init__.py":
            module.__package__ = module.__name__
            module.__path__ = []  # type: ignore[attr-defined]
        else:
            module.__package__ = module.__name__.rsplit(".", 1)[0]
        exec(compile(record.content, record.absolute_path, "exec"), module.__dict__)

    def _module_name_for_record(self, record: ModuleSourceFile) -> str | None:
        if record.module_path == "__init__.py" or record.module_path.endswith("/__init__.py"):
            if record.module_path != self.entry_module_path:
                return None
            parent = record.module_path[: -len("/__init__.py")] if record.module_path.endswith("/__init__.py") else ""
            suffix = ".".join(part for part in parent.split("/") if part)
            return f"{self.package_name}.{suffix}" if suffix else self.package_name
        if not record.module_path.endswith(".py"):
            return None
        relative = record.module_path[: -len(".py")]
        suffix = ".".join(part for part in relative.split("/") if part)
        return f"{self.package_name}.{suffix}" if suffix else self.package_name

    def _add_package_dirs(self, module_path: str) -> None:
        parts = module_path.split("/")[:-1]
        prefix = self.package_name
        for part in parts:
            prefix = f"{prefix}.{part}"
            self._packages.add(prefix)


class ModuleLoader:
    """Loads trusted startup module manifests and Python entrypoints."""

    MANIFEST_FIELDS = {
        "schema_version",
        "module_id",
        "name",
        "version",
        "entrypoint",
        "provides",
        "metadata",
        "sha256",
    }
    PROVIDES_FIELDS = {
        "tools",
        "images",
        "syscalls",
        "provider_hooks",
        "startup_hooks",
        "durable_object_release_finalizers",
    }

    def __init__(
        self,
        config: AgentLibOSConfig | None = None,
        *,
        trusted_modules: tuple[str, ...] = (),
        trusted_sha256: tuple[str, ...] = (),
    ) -> None:
        self.config = config or DEFAULT_CONFIG
        self.trusted_modules = tuple(self.config.modules.trusted_modules) + tuple(trusted_modules)
        self.trusted_sha256 = tuple(self.config.modules.trusted_sha256) + tuple(trusted_sha256)

    def load(self, manifest_path: str | Path) -> tuple[ModuleSource, Any]:
        source = self.resolve(manifest_path)
        if not self.is_trusted(source.manifest.module_id, source.source_sha256, source.manifest_sha256):
            raise CapabilityDenied(
                "startup module is not trusted: "
                f"{source.manifest.module_id}:{source.manifest_sha256}:{source.source_sha256}"
            )
        return source, self.import_entrypoint(source)

    def verify(self, manifest_path: str | Path) -> dict[str, Any]:
        source = self.resolve(manifest_path)
        return {
            "module_id": source.manifest.module_id,
            "name": source.manifest.name,
            "version": source.manifest.version,
            "entrypoint": source.manifest.entrypoint,
            "manifest_path": source.manifest_path,
            "manifest_sha256": source.manifest_sha256,
            "source_path": source.source_path,
            "source_sha256": source.source_sha256,
            "source_kind": source.source_kind,
            "source_root": source.source_root,
            "source_files": self._source_file_summaries(source),
            "trusted": self.is_trusted(source.manifest.module_id, source.source_sha256, source.manifest_sha256),
            "trust_key": self.trust_key(source.manifest.module_id, source.manifest_sha256, source.source_sha256),
            "provides": {
                "tools": list(source.manifest.provides.tools),
                "images": list(source.manifest.provides.images),
                "syscalls": list(source.manifest.provides.syscalls),
                "provider_hooks": list(source.manifest.provides.provider_hooks),
                "startup_hooks": list(source.manifest.provides.startup_hooks),
                "durable_object_release_finalizers": list(
                    source.manifest.provides.durable_object_release_finalizers
                ),
            },
        }

    def resolve(self, manifest_path: str | Path) -> ModuleSource:
        path = Path(manifest_path).expanduser().resolve()
        if not path.is_file():
            raise NotFound(f"module manifest not found: {manifest_path}")
        text = self._read_manifest(path)
        manifest = self.parse_manifest(text)
        source_path, entrypoint_object = self._resolve_entrypoint(path, manifest.entrypoint)
        source_bytes = self._read_source_bytes(source_path)
        source_sha = self._sha256_bytes(source_bytes)
        expected_sha = manifest.sha256.lower()
        source_root = self._infer_source_root(path.parent.resolve(), source_path, manifest.entrypoint)
        source_files = self._entry_source_files(path.parent.resolve(), source_root, source_path, source_bytes)
        source_kind = "file"
        if source_sha != expected_sha:
            source_files = self._read_package_source_files(path.parent.resolve(), source_root)
            package_sha = self._package_sha256(source_files)
            if package_sha != expected_sha:
                raise ValidationError(
                    "module source sha256 mismatch: "
                    f"expected {expected_sha}, got entry={source_sha}, package={package_sha}"
                )
            source_sha = package_sha
            source_kind = "package"
            entry = next((record for record in source_files if Path(record.absolute_path).resolve() == source_path.resolve()), None)
            if entry is None:
                raise ValidationError(f"module entrypoint source is missing from package snapshot: {source_path}")
            source_bytes = entry.content
        if source_kind == "file" and source_sha != expected_sha:
            raise ValidationError(
                "module source sha256 mismatch: "
                f"expected {expected_sha}, got {source_sha}"
            )
        return ModuleSource(
            manifest=manifest,
            manifest_path=str(path),
            manifest_sha256=self._sha256_bytes(text.encode("utf-8")),
            source_path=str(source_path),
            source_sha256=source_sha,
            entrypoint_object=entrypoint_object,
            source_bytes=source_bytes,
            source_kind=source_kind,
            source_root=str(source_root),
            source_files=tuple(source_files),
        )

    def parse_manifest(self, text: str) -> ModuleManifest:
        if len(text.encode("utf-8")) > self.config.modules.manifest_hard_limit_bytes:
            raise ValidationError(
                "module manifest exceeded "
                f"manifest_hard_limit_bytes={self.config.modules.manifest_hard_limit_bytes}"
            )
        data = self._load_mapping(text)
        if set(data) == {"module"} and isinstance(data["module"], dict):
            data = dict(data["module"])
        unknown = sorted(set(data) - self.MANIFEST_FIELDS)
        if unknown:
            raise ValidationError(f"unknown module manifest fields: {unknown}")
        missing = sorted(field for field in ["schema_version", "module_id", "name", "entrypoint", "provides", "sha256"] if field not in data)
        if missing:
            raise ValidationError(f"missing required module manifest fields: {missing}")
        schema_version = data["schema_version"]
        if isinstance(schema_version, bool) or not isinstance(schema_version, int):
            raise ValidationError("schema_version must be an integer")
        if schema_version != self.config.modules.schema_version:
            raise ValidationError(f"unsupported module schema_version: {schema_version}")
        provides = self._coerce_provides(data["provides"])
        manifest = ModuleManifest(
            schema_version=int(schema_version),
            module_id=self._identifier(data["module_id"], "module_id", self.config.modules.id_max_chars),
            name=self._string(data["name"], "name", self.config.modules.name_max_chars),
            version=self._string(data.get("version") or "v0", "version", self.config.modules.version_max_chars),
            entrypoint=self._string(data["entrypoint"], "entrypoint", self.config.modules.entrypoint_max_chars),
            provides=provides,
            sha256=self._sha256(data["sha256"], "sha256"),
            metadata=self._mapping(data.get("metadata"), "metadata"),
        )
        self._validate_provides(manifest.provides)
        return manifest

    def import_entrypoint(self, source: ModuleSource) -> Any:
        module_ref, object_name = self._split_entrypoint(source.manifest.entrypoint)
        with _IMPORT_LOCK:
            if source.source_kind == "package":
                module = self._import_package(source)
            else:
                module = self._import_file(
                    Path(source.source_path),
                    source.manifest.module_id,
                    source.source_sha256,
                    source.source_bytes,
                )
        try:
            self._verify_imported_module_source(module, source)
            entrypoint = getattr(module, object_name, None)
            if not callable(entrypoint):
                raise ValidationError(f"module entrypoint is not callable: {source.manifest.entrypoint}")
            return entrypoint
        except Exception:
            self._cleanup_imported_package(module)
            raise

    @classmethod
    def import_cleanup_for_entrypoint(cls, entrypoint: Any) -> tuple[str, Any] | None:
        module_name = getattr(entrypoint, "__module__", None)
        if not isinstance(module_name, str):
            return None
        module = sys.modules.get(module_name)
        if module is None:
            return None
        cleanup = getattr(module, _IMPORT_CLEANUP_ATTR, None)
        if isinstance(cleanup, tuple) and len(cleanup) == 2:
            return cleanup
        return None

    @classmethod
    def cleanup_imported_package(cls, cleanup: Any) -> None:
        if not isinstance(cleanup, tuple) or len(cleanup) != 2:
            return
        package_name, importer = cleanup
        cls._clear_import_namespace(str(package_name))
        try:
            sys.meta_path.remove(importer)
        except ValueError:
            pass

    @staticmethod
    def trust_key(module_id: str, manifest_sha256: str, source_sha256: str) -> str:
        return f"{module_id}:{manifest_sha256}:{source_sha256}"

    def is_trusted(self, module_id: str, source_sha256: str, manifest_sha256: str) -> bool:
        accepted = {
            self.trust_key(module_id, manifest_sha256, source_sha256),
            f"{module_id}@{manifest_sha256}:{source_sha256}",
        }
        accepted_hashes = {f"{manifest_sha256}:{source_sha256}"}
        return bool(accepted & set(self.trusted_modules)) or bool(accepted_hashes & set(self.trusted_sha256))

    def _read_manifest(self, path: Path) -> str:
        size = path.stat().st_size
        if size > self.config.modules.manifest_max_bytes:
            raise ValidationError(
                "module manifest exceeded "
                f"manifest_max_bytes={self.config.modules.manifest_max_bytes}"
            )
        return path.read_text(encoding="utf-8")

    def _load_mapping(self, text: str) -> dict[str, Any]:
        stripped = text.lstrip()
        if stripped.startswith("{"):
            data = self._load_json_mapping(text)
            if not isinstance(data, dict):
                raise ValidationError("module manifest JSON must be a mapping")
            return data
        return load_yaml_mapping(text)

    def _load_json_mapping(self, text: str) -> dict[str, Any]:
        try:
            data = json.loads(text, object_pairs_hook=_unique_json_object)
        except ValidationError:
            raise
        except json.JSONDecodeError as exc:
            raise ValidationError(f"invalid module manifest JSON: {exc}") from exc
        if not isinstance(data, dict):
            raise ValidationError("module manifest JSON must be a mapping")
        return data

    def _coerce_provides(self, value: Any) -> ModuleProvides:
        if not isinstance(value, dict):
            raise ValidationError("provides must be a mapping")
        unknown = sorted(set(value) - self.PROVIDES_FIELDS)
        if unknown:
            raise ValidationError(f"unknown module provides fields: {unknown}")
        return ModuleProvides(
            tools=self._string_list(value.get("tools"), "provides.tools", self.config.modules.max_declared_tools),
            images=self._string_list(value.get("images"), "provides.images", self.config.modules.max_declared_images),
            syscalls=self._string_list(value.get("syscalls"), "provides.syscalls", self.config.modules.max_declared_syscalls),
            provider_hooks=self._string_list(
                value.get("provider_hooks"),
                "provides.provider_hooks",
                self.config.modules.max_declared_provider_hooks,
            ),
            startup_hooks=self._string_list(
                value.get("startup_hooks"),
                "provides.startup_hooks",
                self.config.modules.max_declared_startup_hooks,
            ),
            durable_object_release_finalizers=self._string_list(
                value.get("durable_object_release_finalizers"),
                "provides.durable_object_release_finalizers",
                self.config.modules.max_declared_startup_hooks,
            ),
        )

    def _validate_provides(self, provides: ModuleProvides) -> None:
        for field, values in {
            "tools": provides.tools,
            "images": provides.images,
            "syscalls": provides.syscalls,
            "provider_hooks": provides.provider_hooks,
            "startup_hooks": provides.startup_hooks,
            "durable_object_release_finalizers": (
                provides.durable_object_release_finalizers
            ),
        }.items():
            duplicates = sorted({value for value in values if values.count(value) > 1})
            if duplicates:
                raise ValidationError(f"duplicate module provides.{field}: {duplicates}")
        for name in provides.syscalls:
            if not _SYSCALL_PATTERN.match(name):
                raise ValidationError(f"invalid syscall name in module manifest: {name}")
        for field, values in {
            "provider_hooks": provides.provider_hooks,
            "startup_hooks": provides.startup_hooks,
            "durable_object_release_finalizers": (
                provides.durable_object_release_finalizers
            ),
        }.items():
            for name in values:
                if not _SYSCALL_PATTERN.match(name):
                    raise ValidationError(f"invalid {field[:-1]} name in module manifest: {name}")

    def _resolve_entrypoint(self, manifest_path: Path, entrypoint: str) -> tuple[Path, str]:
        module_ref, object_name = self._split_entrypoint(entrypoint)
        if self._is_path_ref(module_ref):
            source = (manifest_path.parent / module_ref).resolve()
            self._require_under(source, manifest_path.parent.resolve())
        else:
            source = self._resolve_import_source(manifest_path.parent.resolve(), module_ref)
        if not source.is_file():
            raise NotFound(f"module entrypoint source not found: {source}")
        return source, object_name

    def _split_entrypoint(self, entrypoint: str) -> tuple[str, str]:
        if ":" not in entrypoint:
            raise ValidationError("module entrypoint must use '<module-or-path>:<callable>'")
        module_ref, object_name = entrypoint.rsplit(":", 1)
        module_ref = module_ref.strip()
        object_name = object_name.strip()
        if not module_ref or not object_name:
            raise ValidationError("module entrypoint must include both module/path and callable")
        if not self._is_path_ref(module_ref) and not _PYTHON_MODULE_PATTERN.match(module_ref):
            raise ValidationError(f"module entrypoint import is not a valid Python module path: {module_ref}")
        if not _PYTHON_OBJECT_PATTERN.match(object_name):
            raise ValidationError(f"module entrypoint callable is not a valid Python identifier: {object_name}")
        return module_ref, object_name

    def _is_path_ref(self, module_ref: str) -> bool:
        return module_ref.endswith(".py") or module_ref.startswith(".") or "/" in module_ref or "\\" in module_ref

    def _resolve_import_source(self, manifest_dir: Path, module_ref: str) -> Path:
        """Resolve import-string entrypoints without executing package code."""

        parts = module_ref.split(".")
        self._require_package_parent_files(manifest_dir, parts[:-1], module_ref)
        module_path = manifest_dir.joinpath(*parts)
        file_source = module_path.with_suffix(".py")
        package_source = module_path / "__init__.py"
        if file_source.is_file():
            source = file_source.resolve()
            self._require_under(source, manifest_dir)
            return source
        if package_source.is_file():
            source = package_source.resolve()
            self._require_under(source, manifest_dir)
            return source
        raise NotFound(f"module entrypoint import not found under manifest directory: {module_ref}")

    def _require_package_parent_files(self, manifest_dir: Path, package_parts: list[str], module_ref: str) -> None:
        for index in range(1, len(package_parts) + 1):
            init_path = manifest_dir.joinpath(*package_parts[:index], "__init__.py")
            if not init_path.is_file():
                package_name = ".".join(package_parts[:index])
                raise NotFound(
                    "module entrypoint import requires package parent "
                    f"{package_name!r} with __init__.py under the manifest directory: {module_ref}"
                )
            self._require_under(init_path.resolve(), manifest_dir)

    def _require_under(self, path: Path, root: Path) -> None:
        try:
            path.relative_to(root)
        except ValueError as exc:
            raise ValidationError(f"module entrypoint path escapes manifest directory: {path}") from exc

    def _infer_source_root(self, manifest_dir: Path, source_path: Path, entrypoint: str) -> Path:
        module_ref, _object_name = self._split_entrypoint(entrypoint)
        source = source_path.resolve()
        self._require_under(source, manifest_dir)
        if not self._is_path_ref(module_ref):
            first = module_ref.split(".", 1)[0]
            candidate = (manifest_dir / first).resolve()
            if candidate.is_dir():
                self._require_under(candidate, manifest_dir)
                return candidate
        if source.name == "__init__.py":
            return source.parent
        current = source.parent
        root = current
        while current != manifest_dir and (current / "__init__.py").is_file():
            root = current
            current = current.parent
        return root

    def _entry_source_files(
        self,
        manifest_dir: Path,
        source_root: Path,
        source_path: Path,
        source_bytes: bytes,
    ) -> tuple[ModuleSourceFile, ...]:
        relative = self._manifest_relative_path(manifest_dir, source_path.resolve())
        module_path = self._source_root_relative_path(source_root.resolve(), source_path.resolve())
        return (
            ModuleSourceFile(
                path=relative,
                module_path=module_path,
                absolute_path=str(source_path.resolve()),
                size_bytes=len(source_bytes),
                sha256=self._sha256_bytes(source_bytes),
                content=source_bytes,
            ),
        )

    def _read_package_source_files(self, manifest_dir: Path, source_root: Path) -> tuple[ModuleSourceFile, ...]:
        root = source_root.resolve()
        self._require_under(root, manifest_dir)
        records: list[ModuleSourceFile] = []
        total_bytes = 0
        for item in sorted(root.rglob("*")):
            try:
                source_relative_parts = item.relative_to(root).parts
            except ValueError as exc:
                raise ValidationError(f"module package path escapes source root: {item}") from exc
            if any(part.lower() in _CACHE_PACKAGE_SEGMENTS for part in source_relative_parts):
                continue
            before = item.lstat()
            relative = self._manifest_relative_path(manifest_dir, item.resolve() if not stat.S_ISLNK(before.st_mode) else item)
            self._validate_source_relative_path(relative)
            if stat.S_ISLNK(before.st_mode):
                raise ValidationError(f"module package symlinks are not supported: {item}")
            if stat.S_ISDIR(before.st_mode):
                continue
            if not stat.S_ISREG(before.st_mode):
                raise ValidationError(f"module package path is not a regular file or directory: {item}")
            if before.st_nlink > 1:
                raise ValidationError(f"module package hard links are not supported: {item}")
            if item.suffix != ".py":
                continue
            content = self._read_source_bytes(item)
            total_bytes += len(content)
            if total_bytes > self.config.modules.package_max_bytes:
                raise ValidationError(
                    "module package exceeded "
                    f"package_max_bytes={self.config.modules.package_max_bytes}"
                )
            records.append(
                ModuleSourceFile(
                    path=relative,
                    module_path=self._source_root_relative_path(root, item.resolve()),
                    absolute_path=str(item.resolve()),
                    size_bytes=len(content),
                    sha256=self._sha256_bytes(content),
                    content=content,
                )
            )
            if len(records) > self.config.modules.max_package_files:
                raise ValidationError(
                    "module package exceeded "
                    f"max_package_files={self.config.modules.max_package_files}"
                )
        if not records:
            raise NotFound(f"module package contains no Python source files: {root}")
        return tuple(sorted(records, key=lambda record: record.path))

    def _manifest_relative_path(self, manifest_dir: Path, path: Path) -> str:
        try:
            return path.relative_to(manifest_dir).as_posix()
        except ValueError as exc:
            raise ValidationError(f"module package path escapes manifest directory: {path}") from exc

    def _source_root_relative_path(self, source_root: Path, path: Path) -> str:
        try:
            return path.relative_to(source_root).as_posix()
        except ValueError as exc:
            raise ValidationError(f"module package path escapes source root: {path}") from exc

    def _validate_source_relative_path(self, path: str) -> None:
        normalized = path.replace("\\", "/").strip()
        if not normalized or normalized.startswith("/") or ":" in normalized.split("/", 1)[0]:
            raise ValidationError(f"module package path must be relative: {path!r}")
        parts: list[str] = []
        for part in normalized.split("/"):
            if part in {"", "."}:
                continue
            if part == "..":
                raise ValidationError(f"module package path escapes source root: {path!r}")
            parts.append(part)
        if not parts or "/".join(parts) != normalized:
            raise ValidationError(f"module package path must be normalized: {path!r}")
        if any(ord(char) < 32 for char in normalized):
            raise ValidationError(f"module package path contains control characters: {path!r}")
        for part in parts:
            lower = part.lower()
            stem = part.split(".", 1)[0].upper()
            if any(char in _WINDOWS_FORBIDDEN_PATH_CHARS for char in part):
                raise ValidationError(f"module package path contains a Windows-unsafe character: {path!r}")
            if part.endswith((" ", ".")):
                raise ValidationError(f"module package path contains a Windows-unsafe segment: {path!r}")
            if stem in _WINDOWS_RESERVED_PATH_NAMES:
                raise ValidationError(f"module package path uses a reserved Windows device name: {path!r}")
            if lower in _CACHE_PACKAGE_SEGMENTS:
                raise ValidationError(f"module package must not include cache or VCS paths: {path!r}")
            if lower in _SENSITIVE_PACKAGE_FILENAMES or lower.endswith(_SENSITIVE_PACKAGE_SUFFIXES):
                raise ValidationError(f"module package must not include likely secret material: {path!r}")

    def _package_sha256(self, source_files: tuple[ModuleSourceFile, ...]) -> str:
        canonical = [
            {"path": record.path, "size_bytes": record.size_bytes, "sha256": record.sha256}
            for record in source_files
        ]
        payload = {"kind": "agent_libos_runtime_module_package", "files": canonical}
        return self._sha256_bytes(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8"))

    def _source_file_summaries(self, source: ModuleSource) -> list[dict[str, Any]]:
        return [
            {
                "path": record.path,
                "module_path": record.module_path,
                "size_bytes": record.size_bytes,
                "sha256": record.sha256,
            }
            for record in source.source_files
        ]

    def _import_file(self, path: Path, module_id: str, source_sha256: str, source_bytes: bytes) -> ModuleType:
        module_name = (
            "_agent_libos_module_"
            f"{hashlib.sha256((module_id + str(path) + source_sha256).encode('utf-8')).hexdigest()}"
        )
        return self._exec_source_module(module_name, path, source_bytes)

    def _import_package(self, source: ModuleSource) -> ModuleType:
        package_name = (
            "_agent_libos_module_pkg_"
            f"{hashlib.sha256((source.manifest.module_id + source.source_root + source.source_sha256).encode('utf-8')).hexdigest()}"
            f"_{new_id('load')}"
        )
        entry_module_path = self._source_root_relative_path(Path(source.source_root).resolve(), Path(source.source_path).resolve())
        importer = _SnapshotPackageImporter(package_name, tuple(source.source_files), entry_module_path)
        if entry_module_path == "__init__.py":
            entry_name = package_name
        elif entry_module_path.endswith("/__init__.py"):
            entry_name = f"{package_name}.{entry_module_path[:-len('/__init__.py')].replace('/', '.')}"
        else:
            entry_name = f"{package_name}.{entry_module_path[:-3].replace('/', '.')}"
        self._clear_import_namespace(package_name)
        sys.meta_path.insert(0, importer)
        try:
            module = importlib.import_module(entry_name)
            setattr(module, _IMPORT_CLEANUP_ATTR, (package_name, importer))
            return module
        except Exception:
            self._clear_import_namespace(package_name)
            try:
                sys.meta_path.remove(importer)
            except ValueError:
                pass
            raise

    def _cleanup_imported_package(self, module: ModuleType) -> None:
        self.cleanup_imported_package(getattr(module, _IMPORT_CLEANUP_ATTR, None))

    @staticmethod
    def _clear_import_namespace(module_name: str) -> None:
        for name in list(sys.modules):
            if name == module_name or name.startswith(f"{module_name}."):
                sys.modules.pop(name, None)

    def _exec_source_module(self, module_name: str, path: Path, source_bytes: bytes) -> ModuleType:
        spec = importlib.util.spec_from_loader(module_name, loader=None, origin=str(path))
        if spec is None:
            raise ValidationError(f"cannot import module entrypoint source: {path}")
        module = ModuleType(module_name)
        module.__file__ = str(path)
        module.__spec__ = spec
        module.__package__ = ""
        sys.modules[module_name] = module
        try:
            exec(compile(source_bytes, str(path), "exec"), module.__dict__)
        finally:
            sys.modules.pop(module_name, None)
        return module

    def _verify_imported_module_source(self, module: ModuleType, source: ModuleSource) -> None:
        module_file = getattr(module, "__file__", None)
        if module_file is None:
            raise ValidationError(f"module entrypoint has no source file: {source.manifest.entrypoint}")
        imported_path = Path(module_file).resolve()
        expected_path = Path(source.source_path).resolve()
        if imported_path != expected_path:
            raise ValidationError(
                "module entrypoint import resolved to a different source file: "
                f"expected {expected_path}, got {imported_path}"
            )
        if source.source_kind == "package":
            source_root = Path(source.source_root).resolve()
            manifest_dir = Path(source.manifest_path).resolve().parent
            imported_sha = self._package_sha256(self._read_package_source_files(manifest_dir, source_root))
            if imported_sha != source.source_sha256:
                raise ValidationError(
                    "module package source changed after verification: "
                    f"expected {source.source_sha256}, got {imported_sha}"
                )
            return
        imported_sha = self._sha256_file(imported_path)
        if imported_sha != source.source_sha256:
            raise ValidationError(
                "module entrypoint source changed after verification: "
                f"expected {source.source_sha256}, got {imported_sha}"
            )

    def _identifier(self, value: Any, field: str, max_chars: int) -> str:
        text = self._string(value, field, max_chars)
        if not _MODULE_ID_PATTERN.match(text):
            raise ValidationError(f"{field} contains unsupported characters: {text!r}")
        return text

    def _string(self, value: Any, field: str, max_chars: int) -> str:
        if not isinstance(value, str) or not value.strip():
            raise ValidationError(f"{field} must be a non-empty string")
        text = value.strip()
        if len(text) > max_chars:
            raise ValidationError(f"{field} exceeds max length {max_chars}")
        if any(ord(char) < 32 for char in text):
            raise ValidationError(f"{field} contains control characters")
        return text

    def _string_list(self, value: Any, field: str, max_items: int) -> list[str]:
        if value is None:
            return []
        if not isinstance(value, list):
            raise ValidationError(f"{field} must be a list")
        if len(value) > max_items:
            raise ValidationError(f"{field} exceeds max item count {max_items}")
        return [self._string(item, f"{field}[]", self.config.modules.id_max_chars) for item in value]

    def _mapping(self, value: Any, field: str) -> dict[str, Any]:
        if value is None:
            return {}
        if not isinstance(value, dict):
            raise ValidationError(f"{field} must be a mapping")
        return dict(value)

    def _sha256(self, value: Any, field: str) -> str:
        if not isinstance(value, str) or not _HEX_SHA256_PATTERN.match(value):
            raise ValidationError(f"{field} must be a sha256 hex digest")
        return value.lower()

    def _sha256_file(self, path: Path) -> str:
        return self._sha256_bytes(self._read_source_bytes(path))

    def _read_source_bytes(self, path: Path) -> bytes:
        if not path.exists() or not path.is_file():
            raise NotFound(f"module source not found: {path}")
        before = path.lstat()
        if stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode):
            raise ValidationError(f"module source is not a regular file: {path}")
        if before.st_nlink > 1:
            raise ValidationError(f"module source hard links are not supported: {path}")
        if before.st_size > self.config.modules.source_max_bytes:
            raise ValidationError(
                "module source exceeded "
                f"source_max_bytes={self.config.modules.source_max_bytes}"
            )
        try:
            fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        except OSError as exc:
            if exc.errno == errno.ELOOP:
                raise ValidationError(f"module source symlinks are not supported: {path}") from exc
            raise
        with os.fdopen(fd, "rb") as handle:
            opened = os.fstat(handle.fileno())
            if not stat.S_ISREG(opened.st_mode):
                raise ValidationError(f"module source is not a regular file: {path}")
            if opened.st_nlink > 1:
                raise ValidationError(f"module source hard links are not supported: {path}")
            if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
                raise ValidationError(f"module source changed during read: {path}")
            raw = handle.read()
        if len(raw) > self.config.modules.source_max_bytes:
            raise ValidationError(
                "module source exceeded "
                f"source_max_bytes={self.config.modules.source_max_bytes}"
            )
        return raw

    def _sha256_bytes(self, value: bytes) -> str:
        return hashlib.sha256(value).hexdigest()


def _unique_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    mapping: dict[str, Any] = {}
    for key, value in pairs:
        if key in mapping:
            raise ValidationError(f"duplicate module manifest JSON key: {key!r}")
        mapping[key] = value
    return mapping
