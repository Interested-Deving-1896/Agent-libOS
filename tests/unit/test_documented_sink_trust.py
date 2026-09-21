from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def test_documented_sink_trust_example_runs_without_a_provider_request() -> None:
    completed = subprocess.run(
        [sys.executable, str(ROOT / "examples/data_flow/sink_trust_demo.py")],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert completed.returncode == 0, completed.stdout + completed.stderr
    result = json.loads(completed.stdout)
    assert result["registration_without_admin"] == "denied"
    assert result["before_registration"] == "deny"
    assert result["after_registration"] == "allow"
    assert result["after_profile_change"] == "deny"
    assert result["provider_requests"] == 0
