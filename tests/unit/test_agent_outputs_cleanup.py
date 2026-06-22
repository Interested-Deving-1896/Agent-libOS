from __future__ import annotations

from scripts.agent_outputs import cleanup_agent_outputs, snapshot_agent_outputs


class TestAgentOutputsCleanup:
    def test_cleanup_removes_only_paths_outside_baseline(self, tmp_path) -> None:
        root = tmp_path / "agent_outputs"
        preserved = root / "existing.txt"
        generated = root / "generated" / "file.txt"
        preserved.parent.mkdir()
        preserved.write_text("keep", encoding="utf-8")
        baseline = snapshot_agent_outputs(root)
        generated.parent.mkdir()
        generated.write_text("remove", encoding="utf-8")

        removed = cleanup_agent_outputs(root, baseline=baseline)

        assert "generated/file.txt" in removed
        assert "generated/" in removed
        assert preserved.read_text(encoding="utf-8") == "keep"
        assert not generated.exists()

    def test_cleanup_dry_run_reports_without_deleting(self, tmp_path) -> None:
        root = tmp_path / "agent_outputs"
        generated = root / "generated.txt"
        root.mkdir()
        generated.write_text("remove", encoding="utf-8")

        removed = cleanup_agent_outputs(root, dry_run=True)

        assert "generated.txt" in removed
        assert generated.exists()

