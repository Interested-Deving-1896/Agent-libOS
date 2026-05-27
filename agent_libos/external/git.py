from __future__ import annotations

from pathlib import Path

from agent_libos.external.shell import ShellAdapter


class GitAdapter:
    def __init__(self, shell: ShellAdapter):
        self.shell = shell

    def status(self, pid: str) -> str:
        return self.shell.run(pid, ["git", "status", "--short"]).stdout

    def diff(self, pid: str, path: str | Path | None = None) -> str:
        argv = ["git", "diff"]
        if path is not None:
            argv.append(str(path))
        return self.shell.run(pid, argv).stdout

