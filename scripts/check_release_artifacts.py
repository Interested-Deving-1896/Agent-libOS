from __future__ import annotations

import argparse
import ast
from email import policy
from email.parser import BytesParser
import json
from pathlib import Path, PurePosixPath
import stat
import tarfile
import tomllib
import zipfile


ROOT = Path(__file__).resolve().parents[1]
PROJECT_NAME = "agent-libos"
ARCHIVE_NAME = "agent_libos"
WHEEL_REQUIRED_FILES = frozenset(
    {
        "agent_libos/__init__.py",
        "agent_libos/__main__.py",
        "agent_libos/api/cli.py",
        "agent_libos/api/gui/server.py",
    }
)
SDIST_REQUIRED_FILES = frozenset(
    {
        "LICENSE",
        "PKG-INFO",
        "README.md",
        "pyproject.toml",
        "agent_libos/__init__.py",
        "config.yaml",
        "modules/pty/module.yaml",
        "modules/pty/pty_module.py",
        "skills/swe-agent/SKILL.md",
        "images/mini-swe-agent/IMAGE.yaml",
        "docs/release_status.md",
        "tests/invariants.yaml",
        "scripts/check_release_artifacts.py",
    }
)
SDIST_FORBIDDEN_PARTS = frozenset(
    {
        ".benchmark_runs",
        ".git",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".venv",
        "__pycache__",
        "build",
        "dist",
        "dist-electron",
        "node_modules",
    }
)
SDIST_FORBIDDEN_SUFFIXES = frozenset({".db", ".pyc", ".pyo", ".sqlite"})
ALLOWED_SECRET_FIXTURES = frozenset(
    {
        "benchmarks/runtime_safety/fixtures/basic_repo/.env",
        "benchmarks/runtime_safety/fixtures/basic_repo/config/private.key",
        "benchmarks/runtime_safety/fixtures/basic_repo/secrets/token.txt",
    }
)


def _python_package_version(root: Path) -> str:
    tree = ast.parse((root / "agent_libos" / "__init__.py").read_text(encoding="utf-8"))
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        if any(isinstance(target, ast.Name) and target.id == "__version__" for target in node.targets):
            if isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
                return node.value.value
    raise ValueError("agent_libos.__version__ must be a string literal")


def release_versions(root: Path = ROOT) -> dict[str, str]:
    pyproject = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    uv_lock = tomllib.loads((root / "uv.lock").read_text(encoding="utf-8"))
    project_lock = next(
        (
            package
            for package in uv_lock.get("package", [])
            if package.get("name") == PROJECT_NAME and package.get("source", {}).get("editable") == "."
        ),
        None,
    )
    if project_lock is None:
        raise ValueError(f"uv.lock does not contain the editable {PROJECT_NAME} package")
    versions = {
        "pyproject.toml": str(pyproject["project"]["version"]),
        "agent_libos/__init__.py": _python_package_version(root),
        "uv.lock": str(project_lock["version"]),
    }
    gui_package_path = root / "gui" / "package.json"
    gui_lock_path = root / "gui" / "package-lock.json"
    if gui_package_path.exists() != gui_lock_path.exists():
        raise ValueError("GUI package metadata must include both package.json and package-lock.json")
    if gui_package_path.exists():
        gui_package = json.loads(gui_package_path.read_text(encoding="utf-8"))
        gui_lock = json.loads(gui_lock_path.read_text(encoding="utf-8"))
        gui_lock_root = gui_lock.get("packages", {}).get("", {})
        versions.update(
            {
                "gui/package.json": str(gui_package["version"]),
                "gui/package-lock.json": str(gui_lock["version"]),
                "gui/package-lock.json packages root": str(gui_lock_root["version"]),
            }
        )
    return versions


def validate_version_alignment(root: Path = ROOT) -> str:
    versions = release_versions(root)
    selected = versions["pyproject.toml"]
    mismatches = {source: version for source, version in versions.items() if version != selected}
    if mismatches:
        details = ", ".join(f"{source}={version}" for source, version in mismatches.items())
        raise ValueError(f"release version identifiers do not match {selected}: {details}")
    return selected


def _safe_archive_path(name: str) -> PurePosixPath:
    path = PurePosixPath(name)
    if path.is_absolute() or ".." in path.parts:
        raise ValueError(f"archive contains unsafe path: {name}")
    return path


def _single_artifact(artifact_dir: Path, pattern: str, kind: str) -> Path:
    matches = sorted(artifact_dir.glob(pattern))
    if len(matches) != 1:
        rendered = ", ".join(path.name for path in matches) or "none"
        raise ValueError(f"expected exactly one {kind} matching {pattern}, found: {rendered}")
    return matches[0]


def _validate_wheel(wheel_path: Path, version: str) -> None:
    dist_info = f"{ARCHIVE_NAME}-{version}.dist-info"
    with zipfile.ZipFile(wheel_path) as archive:
        for item in archive.infolist():
            if stat.S_ISLNK(item.external_attr >> 16):
                raise ValueError(f"wheel contains a symbolic link: {item.filename}")
        names = set(archive.namelist())
        for name in names:
            path = _safe_archive_path(name)
            if not path.parts or path.parts[0] not in {"agent_libos", dist_info}:
                raise ValueError(f"wheel contains a non-core top-level path: {name}")
        missing = sorted(WHEEL_REQUIRED_FILES - names)
        if missing:
            raise ValueError(f"wheel is missing core files: {missing}")
        metadata_path = f"{dist_info}/METADATA"
        entry_points_path = f"{dist_info}/entry_points.txt"
        license_path = f"{dist_info}/licenses/LICENSE"
        for required in (metadata_path, entry_points_path, license_path):
            if required not in names:
                raise ValueError(f"wheel is missing {required}")
        metadata = BytesParser(policy=policy.default).parsebytes(archive.read(metadata_path))
        if metadata["Name"] != PROJECT_NAME:
            raise ValueError(f"wheel project name is {metadata['Name']!r}, expected {PROJECT_NAME!r}")
        if metadata["Version"] != version:
            raise ValueError(f"wheel version is {metadata['Version']!r}, expected {version!r}")
        if metadata["Requires-Python"] != ">=3.11":
            raise ValueError("wheel Requires-Python must remain >=3.11")
        entry_points = archive.read(entry_points_path).decode("utf-8")
        expected_entries = {
            "agent-libos = agent_libos.api.cli:cli",
            "agent-libos-gui-server = agent_libos.api.gui.server:main",
        }
        missing_entries = sorted(entry for entry in expected_entries if entry not in entry_points)
        if missing_entries:
            raise ValueError(f"wheel is missing console entry points: {missing_entries}")


def _looks_like_secret_file(path: PurePosixPath) -> bool:
    return path.name == ".env" or path.suffix.lower() in {".key", ".p12", ".pem", ".pfx"} or (
        any(part.lower() in {"secret", "secrets"} for part in path.parts)
        and "token" in path.name.lower()
    )


def _validate_sdist(sdist_path: Path, version: str) -> None:
    prefix = f"{ARCHIVE_NAME}-{version}"
    relative_files: set[str] = set()
    with tarfile.open(sdist_path, mode="r:gz") as archive:
        for member in archive.getmembers():
            path = _safe_archive_path(member.name)
            if not path.parts or path.parts[0] != prefix:
                raise ValueError(f"sdist path is outside {prefix}: {member.name}")
            if member.isdir():
                continue
            if not member.isfile():
                raise ValueError(f"sdist contains a non-regular entry: {member.name}")
            relative = PurePosixPath(*path.parts[1:])
            relative_text = relative.as_posix()
            relative_files.add(relative_text)
            if SDIST_FORBIDDEN_PARTS.intersection(relative.parts):
                raise ValueError(f"sdist contains generated or private path: {relative_text}")
            if relative.suffix.lower() in SDIST_FORBIDDEN_SUFFIXES:
                raise ValueError(f"sdist contains generated file: {relative_text}")
            if _looks_like_secret_file(relative) and relative_text not in ALLOWED_SECRET_FIXTURES:
                raise ValueError(f"sdist contains an undeclared secret-like file: {relative_text}")
        metadata_file = archive.extractfile(f"{prefix}/PKG-INFO")
        if metadata_file is None:
            raise ValueError("sdist PKG-INFO cannot be read")
        metadata = BytesParser(policy=policy.default).parsebytes(metadata_file.read())
        if metadata["Name"] != PROJECT_NAME or metadata["Version"] != version:
            raise ValueError("sdist PKG-INFO does not match the release name and version")
        if metadata["Requires-Python"] != ">=3.11":
            raise ValueError("sdist Requires-Python must remain >=3.11")
    missing = sorted(SDIST_REQUIRED_FILES - relative_files)
    if missing:
        raise ValueError(f"sdist is missing repository release files: {missing}")


def validate_artifacts(artifact_dir: Path, *, root: Path = ROOT) -> tuple[Path, Path, str]:
    version = validate_version_alignment(root)
    wheel = _single_artifact(artifact_dir, f"{ARCHIVE_NAME}-{version}-*.whl", "wheel")
    sdist = _single_artifact(artifact_dir, f"{ARCHIVE_NAME}-{version}.tar.gz", "sdist")
    _validate_wheel(wheel, version)
    _validate_sdist(sdist, version)
    return wheel, sdist, version


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate Agent libOS release versions and built artifacts.")
    parser.add_argument("artifact_dir", nargs="?", type=Path, help="Directory containing one wheel and one sdist.")
    parser.add_argument(
        "--version-only",
        action="store_true",
        help="Check only source version alignment; artifact_dir must be omitted.",
    )
    args = parser.parse_args(argv)
    if args.version_only:
        if args.artifact_dir is not None:
            parser.error("artifact_dir cannot be used with --version-only")
        version = validate_version_alignment()
        print(f"release version identifiers are aligned at {version}")
        return 0
    if args.artifact_dir is None:
        parser.error("artifact_dir is required unless --version-only is used")
    wheel, sdist, version = validate_artifacts(args.artifact_dir.resolve())
    print(f"validated {PROJECT_NAME} {version} artifacts: {wheel.name}, {sdist.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
