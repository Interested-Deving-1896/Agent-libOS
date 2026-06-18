from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory

from agent_libos import Runtime
from agent_libos.substrate import LocalResourceProviderSubstrate


@contextmanager
def temporary_runtime() -> Iterator[Runtime]:
    runtime = Runtime.open("local")
    try:
        yield runtime
    finally:
        runtime.close()


@contextmanager
def workspace_runtime() -> Iterator[tuple[Runtime, Path]]:
    with TemporaryDirectory() as tmp:
        root = Path(tmp)
        runtime = Runtime.open("local", substrate=LocalResourceProviderSubstrate(root))
        try:
            yield runtime, root
        finally:
            runtime.close()
