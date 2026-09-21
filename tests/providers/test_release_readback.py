from __future__ import annotations

import json
from pathlib import Path
import re
import subprocess
import sys

import pytest

from agent_libos import Runtime
from agent_libos.api.cli import run_demo


ROOT = Path(__file__).resolve().parents[2]


def test_release_runbook_verifies_the_real_demo_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Exercise the published verifier, including failures hidden by syntax checks."""
    document = (ROOT / "docs/releasing.md").read_text(encoding="utf-8")
    verifiers = [
        source
        for source in re.findall(r"<<'PY'\n(.*?)\nPY", document, flags=re.DOTALL)
        if 'print("entrypoint-help-and-demo-ok")' in source
    ]
    assert len(verifiers) == 1

    monkeypatch.chdir(tmp_path)
    runtime = Runtime.open("local")
    try:
        report = run_demo(runtime)
    finally:
        runtime.close()

    report_path = tmp_path / "demo.json"

    def verify(payload: object) -> subprocess.CompletedProcess[str]:
        report_path.write_text(json.dumps(payload), encoding="utf-8")
        return subprocess.run(
            [sys.executable, "-", str(report_path)],
            input=verifiers[0],
            text=True,
            capture_output=True,
            check=False,
            timeout=10,
        )

    accepted = verify(report)
    assert accepted.returncode == 0, accepted.stderr
    assert accepted.stdout.strip() == "entrypoint-help-and-demo-ok"

    # The nested final report still says success in these mutations: the release
    # check must honor the CLI's top-level receipt, not that nested payload.
    invalid_reports: list[object] = [
        [],
        {},
        report["final_report"],
        {key: value for key, value in report.items() if key != "target_file_exists"},
        {**report, "target_file_exists": False},
        {**report, "target_file_content_matches": False},
        {**report, "target_file_content_matches": "true"},
        {**report, "target_file_content_matches": 1},
    ]
    for payload in invalid_reports:
        rejected = verify(payload)
        assert rejected.returncode == 1
        assert rejected.stdout == ""
        assert "deterministic demo did not return its successful JSON contract" in rejected.stderr
