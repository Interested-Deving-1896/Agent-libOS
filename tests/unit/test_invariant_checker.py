from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from pytest import MonkeyPatch

from scripts import check_test_invariants as checker


class TestInvariantChecker:

    def test_manifest_loader_accepts_yaml_syntax(self, tmp_path: Path) -> None:
        manifest = tmp_path / "invariants.yaml"
        manifest.write_text(
            """
schema_version: 1
invariants:
  - id: sample-invariant
    title: Sample invariant
    lane: unit
    node_ids:
      - tests/unit/test_sample.py::TestSample::test_regression
    benchmark_attack_classes:
      - sample_attack
benchmark_attack_classes:
  sample_attack: sample-invariant
""".lstrip(),
            encoding="utf-8",
        )

        data = checker._load_manifest(manifest)

        assert data["invariants"][0]["id"] == "sample-invariant"
        assert data["benchmark_attack_classes"]["sample_attack"] == "sample-invariant"

    def test_invariant_nodes_must_exist_and_include_deterministic_regression(self) -> None:
        manifest = {
            "invariants": [
                {
                    "id": "real-only",
                    "title": "Real only",
                    "lane": "security",
                    "node_ids": ["tests/security/test_real.py::TestReal::test_live"],
                    "benchmark_attack_classes": [],
                },
                {
                    "id": "missing-node",
                    "title": "Missing node",
                    "lane": "security",
                    "node_ids": ["tests/security/test_missing.py::TestMissing::test_absent"],
                    "benchmark_attack_classes": [],
                },
            ]
        }
        errors: list[str] = []

        checker._check_invariants(
            manifest,
            collected={"tests/security/test_real.py::TestReal::test_live"},
            deterministic_collected=set(),
            errors=errors,
        )

        assert any("real-only: requires at least one deterministic regression node" in error for error in errors)
        assert any("missing-node: pytest node not collected" in error for error in errors)

    def test_benchmark_attack_class_mapping_must_match_declarations_and_tasks(self, monkeypatch: MonkeyPatch) -> None:
        manifest = {
            "benchmark_attack_classes": {
                "declared_elsewhere": "other-invariant",
                "undeclared": "known-invariant",
                "unknown_owner": "missing-invariant",
            }
        }
        monkeypatch.setattr(
            checker,
            "load_tasks",
            lambda _suite: [
                SimpleNamespace(attack_class="task_without_mapping", source_path=None, id="task-1")
            ],
        )
        errors: list[str] = []

        checker._check_benchmark_attack_classes(
            manifest,
            invariant_ids={"known-invariant", "other-invariant"},
            declared_attack_classes={
                "declared_elsewhere": "known-invariant",
                "missing_top_level": "known-invariant",
            },
            errors=errors,
        )

        assert any("maps to 'other-invariant' but is declared on 'known-invariant'" in error for error in errors)
        assert any("'undeclared' is missing from invariant declarations" in error for error in errors)
        assert any("'unknown_owner' maps to unknown invariant 'missing-invariant'" in error for error in errors)
        assert any("'missing_top_level' is declared on 'known-invariant'" in error for error in errors)
        assert any("task_without_mapping" in error for error in errors)
