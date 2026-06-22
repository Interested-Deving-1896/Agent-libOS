from __future__ import annotations

import argparse

import pytest

from scripts import test_matrix


def _args(**overrides: object) -> argparse.Namespace:
    defaults = {
        "lane": "runtime",
        "run_real_deno": False,
        "run_real_llm": False,
        "workers": "1",
        "dist": "loadfile",
        "max_lane_seconds": test_matrix.DEFAULT_MAX_LANE_SECONDS,
    }
    defaults.update(overrides)
    return argparse.Namespace(**defaults)


class TestTestMatrix:
    def test_pytest_args_default_to_serial_execution(self) -> None:
        command = test_matrix._pytest_args(("tests/runtime",), _args())

        assert command[:4] == [test_matrix.sys.executable, "-m", "pytest", "tests/runtime"]
        assert "-n" not in command
        assert "--dist" not in command

    def test_pytest_args_include_xdist_workers_when_requested(self) -> None:
        command = test_matrix._pytest_args(("tests",), _args(workers="4", dist="load"))

        assert command[:4] == [test_matrix.sys.executable, "-m", "pytest", "tests"]
        assert command[4:8] == ["-n", "4", "--dist", "load"]

    def test_worker_count_accepts_positive_int_auto_and_logical(self) -> None:
        assert test_matrix._worker_count("4") == "4"
        assert test_matrix._worker_count("auto") == "auto"
        assert test_matrix._worker_count("logical") == "logical"

    def test_worker_count_rejects_invalid_values(self) -> None:
        with pytest.raises(argparse.ArgumentTypeError):
            test_matrix._worker_count("0")
        with pytest.raises(argparse.ArgumentTypeError):
            test_matrix._worker_count("maybe")

    def test_gui_lane_rejects_workers(self) -> None:
        parser = argparse.ArgumentParser()

        with pytest.raises(SystemExit):
            test_matrix._validate_args(parser, _args(lane="gui", workers="2"))

