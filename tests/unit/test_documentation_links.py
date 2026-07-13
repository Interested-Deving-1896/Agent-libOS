from __future__ import annotations

import re
from pathlib import Path
import subprocess
import sys
from urllib.parse import unquote, urlsplit


ROOT = Path(__file__).resolve().parents[2]
_DOCUMENT_PATTERNS = (
    "*.md",
    "docs/**/*.md",
    "benchmarks/**/*.md",
    "skills/**/*.md",
    "images/**/*.md",
    "modules/**/*.md",
)
DOCUMENTS = sorted(
    {
        path
        for pattern in _DOCUMENT_PATTERNS
        for path in ROOT.glob(pattern)
        if path.is_file()
    }
)
_LINK = re.compile(r"(?<!!)\[[^\]]+\]\(([^)]+)\)")
_HEADING = re.compile(r"^#{1,6}\s+(.+?)\s*#*\s*$", re.MULTILINE)


def _without_code(text: str) -> str:
    text = re.sub(r"```.*?```", "", text, flags=re.DOTALL)
    text = re.sub(r"~~~.*?~~~", "", text, flags=re.DOTALL)
    return re.sub(r"`[^`\n]*`", "", text)


def _target(raw: str) -> str:
    selected = raw.strip()
    if selected.startswith("<") and ">" in selected:
        return selected[1 : selected.index(">")]
    return selected.split(maxsplit=1)[0]


def _anchors(path: Path) -> set[str]:
    anchors: set[str] = set()
    counts: dict[str, int] = {}
    for heading in _HEADING.findall(path.read_text(encoding="utf-8")):
        plain = re.sub(r"<[^>]+>", "", heading)
        plain = re.sub(r"[`*_~]", "", plain).strip().lower()
        slug = "".join(
            character
            for character in plain
            if character.isalnum() or character in {" ", "-", "_"}
        )
        slug = re.sub(r"\s+", "-", slug)
        count = counts.get(slug, 0)
        counts[slug] = count + 1
        anchors.add(slug if count == 0 else f"{slug}-{count}")
    return anchors


def test_local_documentation_links_and_anchors_resolve() -> None:
    failures: list[str] = []
    for document in DOCUMENTS:
        text = _without_code(document.read_text(encoding="utf-8"))
        for raw in _LINK.findall(text):
            selected = _target(raw)
            parsed = urlsplit(selected)
            if parsed.scheme or parsed.netloc:
                continue
            linked = document if not parsed.path else (document.parent / unquote(parsed.path)).resolve()
            if not linked.exists():
                failures.append(f"{document.relative_to(ROOT)} -> missing {selected}")
                continue
            if parsed.fragment and linked.suffix.lower() == ".md":
                anchor = unquote(parsed.fragment).lower()
                if anchor not in _anchors(linked):
                    failures.append(
                        f"{document.relative_to(ROOT)} -> missing anchor {selected}"
                    )

    assert not failures, "\n" + "\n".join(failures)


def test_cli_reference_tracks_every_top_level_command() -> None:
    completed = subprocess.run(
        [sys.executable, "-m", "agent_libos.api.cli", "--help"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    help_match = re.search(r"\{([^{}]+)\}", completed.stdout)
    assert help_match is not None
    actual = help_match.group(1).split(",")

    cli_reference = (ROOT / "docs" / "cli.md").read_text(encoding="utf-8")
    documented_match = re.search(
        r"## Top-Level Commands\s+```text\n(?P<body>.*?)\n```",
        cli_reference,
        flags=re.DOTALL,
    )
    assert documented_match is not None
    documented = [
        line.split(maxsplit=1)[0]
        for line in documented_match.group("body").splitlines()
        if line.strip()
    ]

    assert documented == actual
