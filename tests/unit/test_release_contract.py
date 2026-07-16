from __future__ import annotations

from pathlib import Path
import re
import tomllib

from scripts.check_release_artifacts import validate_version_alignment


ROOT = Path(__file__).resolve().parents[2]


def test_release_version_identifiers_are_aligned() -> None:
    assert validate_version_alignment(ROOT) == "0.2.1"


def test_release_status_contains_current_version_state_only() -> None:
    text = (ROOT / "docs" / "release_status.md").read_text(encoding="utf-8")
    assert text.startswith("# Agent libOS 0.2.1 Status\n")
    forbidden = {
        "commit id": r"\bcommit\b",
        "dirty state": r"\bdirty\b",
        "worktree state": r"\bwork(?:ing)?[ -]?tree\b",
        "content hash": r"\bsha(?:-?256)?\b",
        "benchmark artifact path": r"\.benchmark_runs/",
        "absolute user path": r"(?:/Users/|/home/|/private/|/tmp/|[A-Za-z]:\\Users\\)",
        "bare hexadecimal identifier": r"\b[0-9a-f]{7,40}\b",
        "calendar date": r"\b20\d{2}-\d{2}-\d{2}\b",
    }
    offenders = [label for label, pattern in forbidden.items() if re.search(pattern, text, re.IGNORECASE)]
    assert offenders == []


def test_release_status_references_do_not_describe_a_metadata_ledger() -> None:
    documents = [ROOT / "README.md", *sorted((ROOT / "docs").glob("*.md"))]
    forbidden = ("commit", "dirty", "worktree", "working-tree", "ledger", "sha-256", "sha256", "exact commands")
    offenders: list[str] = []
    for document in documents:
        lines = document.read_text(encoding="utf-8").splitlines()
        for index, line in enumerate(lines):
            if "release_status.md" not in line:
                continue
            context = " ".join(lines[max(0, index - 1) : index + 2]).lower()
            if any(term in context for term in forbidden):
                offenders.append(f"{document.relative_to(ROOT)}:{index + 1}")
    assert offenders == []


def test_python_wheel_scope_is_the_core_package() -> None:
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert pyproject["tool"]["hatch"]["build"]["targets"]["wheel"]["packages"] == ["agent_libos"]
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "The Python wheel contains the core `agent_libos` package" in readme
