"""Small fail-closed supervisor for untrusted JIT subprocesses.

This module is executed in a dedicated, single-threaded Python interpreter.
It deliberately uses no ``preexec_fn``: on POSIX an inherited death pipe
detects loss of the libOS host, while on Windows the host assigns this process
to a KILL_ON_JOB_CLOSE Job Object before releasing the gate file.  The Deno
child is spawned only after that containment boundary is live.
"""

from __future__ import annotations

import argparse
import ctypes
import os
from pathlib import Path
import selectors
import signal
import subprocess
import sys
import time


_READY_FRAME = b'{"type":"supervisor_ready","version":1}\n'


def _write_ready() -> None:
    # One small os.write keeps the readiness frame ordered before any output
    # from the subsequently spawned Deno child that inherits stdout.
    os.write(sys.stdout.fileno(), _READY_FRAME)


def _spawn(command: list[str]) -> subprocess.Popen[bytes]:
    # Standard handles are inherited from the supervisor.  close_fds keeps the
    # POSIX death-pipe descriptor out of Deno so only the supervisor observes
    # the host lifecycle.
    return subprocess.Popen(command, close_fds=True)


def _death_pipe_closed(selector: selectors.BaseSelector, death_fd: int, timeout: float) -> bool:
    if not selector.select(timeout):
        return False
    try:
        return os.read(death_fd, 1) == b""
    except BlockingIOError:
        return False


def _supervise_posix(command: list[str], death_fd: int) -> int:
    selector = selectors.DefaultSelector()
    selector.register(death_fd, selectors.EVENT_READ)
    try:
        if _death_pipe_closed(selector, death_fd, 0.0):
            return 125
        _write_ready()
        proc = _spawn(command)
        while True:
            returncode = proc.poll()
            if returncode is not None:
                return int(returncode)
            if not _death_pipe_closed(selector, death_fd, 0.05):
                continue
            # The supervisor is the session/process-group leader.  Killing the
            # whole group contains Deno even if it has started descendants.
            try:
                os.killpg(os.getpgrp(), signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                try:
                    proc.kill()
                except ProcessLookupError:
                    pass
                return 137
    finally:
        selector.close()
        os.close(death_fd)


def _wait_for_windows_gate(parent_pid: int, gate_file: Path) -> bool:
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    synchronize = 0x00100000
    wait_object_0 = 0x00000000
    kernel32.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
    kernel32.OpenProcess.restype = ctypes.c_void_p
    kernel32.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
    kernel32.WaitForSingleObject.restype = ctypes.c_uint32
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    kernel32.CloseHandle.restype = ctypes.c_int
    parent_handle = kernel32.OpenProcess(synchronize, False, int(parent_pid))
    if not parent_handle:
        return False
    try:
        while not gate_file.exists():
            if kernel32.WaitForSingleObject(parent_handle, 0) == wait_object_0:
                return False
            time.sleep(0.01)
        return True
    finally:
        kernel32.CloseHandle(parent_handle)


def _supervise_windows(command: list[str], parent_pid: int, gate_file: Path) -> int:
    if not _wait_for_windows_gate(parent_pid, gate_file):
        return 125
    # The host creates the gate only after assigning this supervisor to its
    # KILL_ON_JOB_CLOSE job.  Descendants inherit that job by default.
    _write_ready()
    return int(_spawn(command).wait())


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--parent-pid", type=int, required=True)
    parser.add_argument("--death-fd", type=int)
    parser.add_argument("--gate-file")
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    if args.command and args.command[0] == "--":
        args.command = args.command[1:]
    if not args.command:
        parser.error("missing supervised command")
    return args


def main() -> int:
    args = _parse_args()
    try:
        if os.name == "nt":
            if not args.gate_file:
                raise RuntimeError("Windows supervisor requires --gate-file")
            return _supervise_windows(args.command, args.parent_pid, Path(args.gate_file))
        if os.name == "posix":
            if args.death_fd is None:
                raise RuntimeError("POSIX supervisor requires --death-fd")
            return _supervise_posix(args.command, args.death_fd)
        raise RuntimeError(f"unsupported subprocess containment platform: {os.name}")
    except BaseException as exc:
        print(f"Deno supervisor failed: {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
        return 125


if __name__ == "__main__":
    raise SystemExit(main())
