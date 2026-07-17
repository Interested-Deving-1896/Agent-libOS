from __future__ import annotations

import errno
import os
import sqlite3
import stat
from pathlib import Path
from typing import Any

from agent_libos.config import AgentLibOSConfig
from agent_libos.models.exceptions import ValidationError
from agent_libos.storage.sql import SQLRuntimeStore
from agent_libos.utils.ids import utc_now

try:  # pragma: no cover - Windows fallback is exercised only on non-POSIX hosts.
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None


class _SQLiteRuntimeLease:
    def __init__(self, handle: Any, path: Path) -> None:
        self.handle = handle
        self.path = path


class SQLiteStore(SQLRuntimeStore):
    """SQLite runtime store backend.

    Connection setup, file hardening, and lease behavior remain SQLite-only;
    backend-neutral repositories live in :class:`SQLRuntimeStore`.
    """

    def __init__(self, path: str | Path = ":memory:", *, config: AgentLibOSConfig | None = None):
        selected_path = str(path)
        connection_path = selected_path
        self._lease_handle: Any | None = None
        use_database_lease = False
        if selected_path != ":memory:":
            db_path = Path(selected_path)
            # Resolve existing symlinks and relative aliases before deriving
            # the lock path. Otherwise the same SQLite file can be opened by
            # two runtimes through distinct path spellings and receive two
            # independent lease files.
            canonical_path = db_path.resolve()
            if canonical_path.exists():
                self._preflight_existing_store(canonical_path)
            else:
                db_path.parent.mkdir(parents=True, exist_ok=True)
            connection_path = str(canonical_path)
            self._secure_database_files(canonical_path)
            if fcntl is not None and hasattr(os, "O_NOFOLLOW"):
                self._lease_handle = self._acquire_runtime_lease(canonical_path)
            else:
                # SQLite's kernel-managed EXCLUSIVE lock is crash-recoverable,
                # unlike a create-once fallback lockfile that can survive its
                # owner indefinitely.  This is also the Windows path.
                use_database_lease = True
        conn: sqlite3.Connection | None = None
        try:
            conn = sqlite3.connect(
                connection_path,
                check_same_thread=False,
                timeout=0.0 if use_database_lease else 5.0,
            )
            conn.row_factory = sqlite3.Row
            if use_database_lease:
                self._acquire_exclusive_sqlite_lease(conn, Path(connection_path))
            self._init_store(selected_path, config=config, conn=conn)
        except BaseException:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass
            self._release_runtime_lease()
            raise

    def _preflight_existing_store(self, db_path: Path) -> None:
        """Reject an incompatible store through a read-only connection."""

        conn: sqlite3.Connection | None = None
        try:
            conn = sqlite3.connect(f"{db_path.as_uri()}?mode=ro", uri=True)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA schema_version").fetchone()
            self._require_supported_store_version_for(conn)
        except sqlite3.Error as exc:
            busy_codes = {sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED}
            if getattr(exc, "sqlite_errorcode", None) in busy_codes:
                raise ValidationError(
                    f"runtime store is already open: {db_path}"
                ) from exc
            raise ValidationError(
                f"unable to read SQLite store schema: {db_path}"
            ) from exc
        finally:
            if conn is not None:
                conn.close()

    @classmethod
    def _probe_user_schema_objects(cls, conn: Any) -> set[str]:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'"
        )
        return {str(row["name"]) for row in rows}

    def close(self) -> None:
        try:
            super().close()
        finally:
            self._release_runtime_lease()

    def _acquire_runtime_lease(self, db_path: Path) -> _SQLiteRuntimeLease:
        lease_path = db_path.with_suffix(db_path.suffix + ".runtime.lock")
        if fcntl is None or not hasattr(os, "O_NOFOLLOW"):
            raise ValidationError("secure file runtime leases require fcntl and O_NOFOLLOW")
        flags = os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
        try:
            fd = os.open(str(lease_path), flags, 0o600)
        except OSError as exc:
            if exc.errno in {errno.ELOOP, errno.ENOTDIR, errno.EISDIR}:
                raise ValidationError(f"unsafe runtime lease path: {lease_path}") from exc
            raise ValidationError(f"unable to securely open runtime lease: {lease_path}") from exc

        handle: Any | None = None
        try:
            opened_stat = os.fstat(fd)
            if not stat.S_ISREG(opened_stat.st_mode):
                raise ValidationError(f"runtime lease must be a regular file: {lease_path}")
            self._require_owned_file(opened_stat, lease_path, label="runtime lease")
            os.fchmod(fd, 0o600)
            opened_stat = os.fstat(fd)
            handle = os.fdopen(fd, "r+", encoding="utf-8")
            fd = -1
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                if exc.errno in {errno.EACCES, errno.EAGAIN}:
                    raise ValidationError(f"runtime store is already open: {db_path}") from exc
                raise ValidationError(f"unable to lock runtime lease: {lease_path}") from exc

            path_stat = os.stat(lease_path, follow_symlinks=False)
            if (
                not stat.S_ISREG(path_stat.st_mode)
                or path_stat.st_dev != opened_stat.st_dev
                or path_stat.st_ino != opened_stat.st_ino
            ):
                raise ValidationError(f"unsafe runtime lease path changed while opening: {lease_path}")

            handle.seek(0)
            handle.truncate()
            handle.write(f"{utc_now()}\n{os.getpid()}\n")
            handle.flush()
            os.fsync(handle.fileno())
            return _SQLiteRuntimeLease(handle, lease_path)
        except BaseException:
            if handle is not None:
                try:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                except OSError:
                    pass
                handle.close()
            elif fd >= 0:
                os.close(fd)
            raise

    def _secure_database_files(self, db_path: Path) -> None:
        """Create/tighten the SQLite database and existing sidecars to 0600."""
        if not hasattr(os, "O_NOFOLLOW") or not hasattr(os, "fchmod"):
            return
        flags = os.O_RDWR | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
        try:
            fd = os.open(str(db_path), flags)
        except FileNotFoundError:
            try:
                fd = os.open(str(db_path), flags | os.O_CREAT | os.O_EXCL, 0o600)
            except FileExistsError:
                fd = os.open(str(db_path), flags)
        except OSError as exc:
            if exc.errno in {errno.ELOOP, errno.ENOTDIR, errno.EISDIR}:
                raise ValidationError(f"unsafe SQLite database path: {db_path}") from exc
            raise
        try:
            self._tighten_open_file(fd, db_path, label="SQLite database")
        finally:
            os.close(fd)
        for suffix in ("-journal", "-wal", "-shm"):
            sidecar = Path(f"{db_path}{suffix}")
            try:
                sidecar_fd = os.open(str(sidecar), flags)
            except FileNotFoundError:
                continue
            except OSError as exc:
                if exc.errno in {errno.ELOOP, errno.ENOTDIR, errno.EISDIR}:
                    raise ValidationError(f"unsafe SQLite sidecar path: {sidecar}") from exc
                raise
            try:
                self._tighten_open_file(sidecar_fd, sidecar, label="SQLite sidecar")
            finally:
                os.close(sidecar_fd)

    def _tighten_open_file(self, fd: int, path: Path, *, label: str) -> None:
        opened_stat = os.fstat(fd)
        if not stat.S_ISREG(opened_stat.st_mode):
            raise ValidationError(f"{label} must be a regular file: {path}")
        self._require_owned_file(opened_stat, path, label=label)
        os.fchmod(fd, 0o600)
        path_stat = os.stat(path, follow_symlinks=False)
        if (
            not stat.S_ISREG(path_stat.st_mode)
            or path_stat.st_dev != opened_stat.st_dev
            or path_stat.st_ino != opened_stat.st_ino
        ):
            raise ValidationError(f"unsafe {label} path changed while opening: {path}")

    def _require_owned_file(self, opened_stat: os.stat_result, path: Path, *, label: str) -> None:
        if hasattr(os, "getuid") and opened_stat.st_uid != os.getuid():
            raise ValidationError(f"{label} is not owned by the current user: {path}")

    def _acquire_exclusive_sqlite_lease(self, conn: sqlite3.Connection, db_path: Path) -> None:
        try:
            row = conn.execute("PRAGMA locking_mode=EXCLUSIVE").fetchone()
            if row is None or str(row[0]).lower() != "exclusive":
                raise ValidationError(f"SQLite refused exclusive runtime lease mode: {db_path}")
            conn.execute("BEGIN EXCLUSIVE")
            conn.commit()
        except sqlite3.Error as exc:
            try:
                conn.rollback()
            except sqlite3.Error:
                pass
            busy_codes = {sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED}
            if getattr(exc, "sqlite_errorcode", None) in busy_codes:
                raise ValidationError(f"runtime store is already open: {db_path}") from exc
            raise ValidationError(f"unable to acquire SQLite runtime lease: {db_path}") from exc

    def _release_runtime_lease(self) -> None:
        lease = getattr(self, "_lease_handle", None)
        if lease is None:
            return
        self._lease_handle = None
        handle = lease.handle
        if fcntl is not None:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
        handle.close()
