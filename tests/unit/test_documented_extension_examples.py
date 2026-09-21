"""Run the documented extension commands as standalone, token-free examples."""

from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys

import pytest


@pytest.mark.parametrize(
    ("filename", "expected"),
    [
        (
            "python_tool.py",
            {
                "denied_before_grant": True,
                "result": {"bytes_read": 6, "truncated": False},
                "tool_visible": True,
            },
        ),
        (
            "protected_operation.py",
            {
                "audit_and_event_linked": True,
                "denied_after_one_use": True,
                "effect_state": "committed",
                "provider_calls": 1,
                "result": "hello",
            },
        ),
    ],
)
def test_documented_extension_command(filename: str, expected: dict[str, object]) -> None:
    root = Path(__file__).resolve().parents[2]
    completed = subprocess.run(
        [sys.executable, str(root / "examples" / "extensions" / filename)],
        cwd=root,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stderr == ""
    assert json.loads(completed.stdout) == expected
