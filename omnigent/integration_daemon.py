"""Background-daemon lifecycle for CLI-managed integration processes.

Backs ``omni integration slack [--background|status|stop|logs]``. A single daemon
per machine is tracked by a small JSON record (PID + log path + start time)
under the runtime data dir. The daemon itself is an ordinary subprocess (e.g.
``python -m omnigent_slack``); this module only owns spawning it detached,
recording it, checking liveness, and tearing it down — it holds no
integration-specific knowledge.
"""

from __future__ import annotations

import contextlib
import json
import os
import secrets
import signal
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from omnigent.inner import _proc

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows has no flock.
    fcntl = None  # type: ignore[assignment]


@dataclass(frozen=True, slots=True)
class DaemonRecord:
    """A running (or last-known) background daemon.

    :param pid: Spawned process id.
    :param log_path: Absolute path to the daemon's combined stdout/stderr log.
    :param started_at: Unix epoch seconds when the daemon was spawned.
    """

    pid: int
    log_path: str
    started_at: int
    identity: str | None = None


class IntegrationDaemon:
    """Manage one named background daemon tracked by a PID record.

    :param name: Stable identifier, e.g. ``"slack"``. Names the record file,
        the log destination, and user-facing messages.
    :param state_dir: Directory the record lives in (honors the caller's
        data-dir resolution, so tests can isolate via ``OMNIGENT_DATA_DIR``).
    """

    def __init__(self, name: str, state_dir: Path) -> None:
        self.name = name
        self._record_path = state_dir / "integrations" / f"{name}.json"
        self._owner_path = state_dir / "integrations" / f"{name}.lock"
        self._reclaim_path = state_dir / "integrations" / f"{name}.lock.reclaim"
        env_name = "".join(character if character.isalnum() else "_" for character in name)
        self._owner_fd_env = f"OMNIGENT_{env_name.upper()}_DAEMON_OWNER_FD"
        self._owner_token_env = f"OMNIGENT_{env_name.upper()}_DAEMON_OWNER_TOKEN"
        self._owner_fd: int | None = None
        self._owner_token: str | None = None

    # ── Record persistence ────────────────────────────────────────

    def read_record(self) -> DaemonRecord | None:
        """Return the recorded daemon, or ``None`` if absent/malformed."""
        try:
            raw = json.loads(self._record_path.read_text())
        except (OSError, json.JSONDecodeError):
            return None
        if not isinstance(raw, dict):
            return None
        try:
            pid = int(raw["pid"])
            log_path = str(raw["log_path"])
            started_at = int(raw["started_at"])
        except (KeyError, TypeError, ValueError):
            return None
        identity = raw.get("identity")
        if identity is not None and not isinstance(identity, str):
            return None
        return DaemonRecord(pid=pid, log_path=log_path, started_at=started_at, identity=identity)

    def _write_record(self, record: DaemonRecord) -> None:
        self._record_path.parent.mkdir(parents=True, exist_ok=True)
        payload: dict[str, int | str] = {
            "pid": record.pid,
            "log_path": record.log_path,
            "started_at": record.started_at,
        }
        if record.identity is not None:
            payload["identity"] = record.identity
        self._record_path.write_text(json.dumps(payload))

    def _clear_record(self) -> None:
        self._record_path.unlink(missing_ok=True)

    def _owner(self) -> tuple[int, str] | None:
        try:
            raw = json.loads(self._owner_path.read_text())
            token = raw["token"]
            if not isinstance(token, str) or not token:
                return None
            return int(raw["pid"]), token
        except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError):
            return None

    def _write_owner(self, fd: int, pid: int, token: str) -> None:
        payload = json.dumps({"pid": pid, "token": token}).encode()
        os.lseek(fd, 0, os.SEEK_SET)
        os.ftruncate(fd, 0)
        os.write(fd, payload)
        os.fsync(fd)

    def _acquire_owner(self) -> None:
        if self._owner_fd is not None or self._owner_token is not None:
            return
        self._owner_path.parent.mkdir(parents=True, exist_ok=True)
        pid = os.getpid()
        token = os.environ.pop(self._owner_token_env, None) or secrets.token_urlsafe(24)
        if fcntl is not None:
            inherited = os.environ.pop(self._owner_fd_env, None)
            if inherited is not None:
                try:
                    fd = int(inherited)
                    if os.fstat(fd).st_ino != self._owner_path.stat().st_ino:
                        raise OSError("owner fd does not match lock file")
                except (OSError, ValueError) as exc:
                    raise RuntimeError(f"invalid {self.name} daemon ownership handoff") from exc
                os.set_inheritable(fd, False)
                self._owner_fd = fd
                self._write_owner(fd, pid, token)
                return
            fd = os.open(self._owner_path, os.O_CREAT | os.O_RDWR, 0o600)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                os.close(fd)
                owner = self._owner()
                suffix = f" (pid {owner[0]})" if owner else ""
                raise RuntimeError(f"{self.name} daemon already running{suffix}") from exc
            self._owner_fd = fd
            self._write_owner(fd, pid, token)
            return

        for _ in range(3):  # pragma: no cover - Windows fallback.
            try:
                fd = os.open(self._owner_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            except FileExistsError as exc:
                owner = self._owner()
                if owner and owner[1] == token:
                    self._owner_path.write_text(json.dumps({"pid": pid, "token": token}))
                    self._owner_token = token
                    return
                if owner and self._pid_alive(owner[0]):
                    raise RuntimeError(
                        f"{self.name} daemon already running (pid {owner[0]})"
                    ) from exc
                try:
                    guard_fd = os.open(
                        self._reclaim_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600
                    )
                except FileExistsError as guard_exc:
                    raise RuntimeError(f"{self.name} daemon ownership is being reclaimed") from (
                        guard_exc
                    )
                try:
                    with os.fdopen(guard_fd, "w") as handle:
                        json.dump({"pid": pid, "token": token}, handle)
                    owner = self._owner()
                    if owner and self._pid_alive(owner[0]):
                        raise RuntimeError(
                            f"{self.name} daemon already running (pid {owner[0]})"
                        ) from exc
                    self._owner_path.unlink(missing_ok=True)
                finally:
                    self._reclaim_path.unlink(missing_ok=True)
                continue
            with os.fdopen(fd, "w") as handle:
                json.dump({"pid": pid, "token": token}, handle)
            self._owner_token = token
            return
        raise RuntimeError(f"could not claim {self.name} daemon ownership")

    def _release_owner(self, *, force: bool = False) -> None:
        if self._owner_fd is not None:
            fd, self._owner_fd = self._owner_fd, None
            self._owner_path.unlink(missing_ok=True)
            if fcntl is not None:
                with contextlib.suppress(OSError):
                    fcntl.flock(fd, fcntl.LOCK_UN)
            with contextlib.suppress(OSError):
                os.close(fd)
            return
        if fcntl is not None:
            cleanup_fd: int | None = None
            try:
                cleanup_fd = os.open(self._owner_path, os.O_CREAT | os.O_RDWR, 0o600)
                fcntl.flock(cleanup_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                if cleanup_fd is not None:
                    with contextlib.suppress(OSError):
                        os.close(cleanup_fd)
                return
            assert cleanup_fd is not None
            self._owner_path.unlink(missing_ok=True)
            with contextlib.suppress(OSError):
                fcntl.flock(cleanup_fd, fcntl.LOCK_UN)
            with contextlib.suppress(OSError):
                os.close(cleanup_fd)
            return
        owner = self._owner()
        if force or (owner and owner == (os.getpid(), self._owner_token)):
            self._owner_path.unlink(missing_ok=True)
        self._owner_token = None

    def acquire_current(self, log_path: str | Path) -> DaemonRecord:
        """Claim daemon ownership for this process or accept its pre-spawn record."""
        current = self.running_record()
        pid = os.getpid()
        if current is not None:
            if current.pid != pid:
                raise RuntimeError(f"{self.name} daemon already running (pid {current.pid})")
        self._acquire_owner()
        if current is not None:
            return current
        record = DaemonRecord(
            pid=pid,
            log_path=str(log_path),
            started_at=int(time.time()),
            identity=self._pid_identity(pid),
        )
        try:
            self._write_record(record)
        except BaseException:
            self._release_owner()
            raise
        return record

    def release_current(self) -> None:
        """Release ownership only when this process owns the daemon record."""
        record = self.read_record()
        if record is not None and record.pid == os.getpid():
            self._clear_record()
            self._release_owner()

    # ── Liveness ──────────────────────────────────────────────────

    @staticmethod
    def _pid_alive(pid: int) -> bool:
        """Whether *pid* names a live process (best-effort, POSIX/Windows)."""
        if pid <= 0:
            return False
        if os.name == "nt":
            return IntegrationDaemon._pid_identity(pid) is not None
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            # Exists but owned by another user — still "alive" for our purposes.
            return True
        except OSError:
            return False
        return True

    @staticmethod
    def _pid_identity(pid: int) -> str | None:
        if os.name != "nt":
            return None
        try:  # pragma: no cover - exercised on Windows.
            import ctypes
            from ctypes import wintypes

            kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
            kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
            kernel32.OpenProcess.restype = wintypes.HANDLE
            filetime = ctypes.POINTER(wintypes.FILETIME)
            kernel32.GetProcessTimes.argtypes = [
                wintypes.HANDLE,
                filetime,
                filetime,
                filetime,
                filetime,
            ]
            kernel32.GetProcessTimes.restype = wintypes.BOOL
            kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
            kernel32.CloseHandle.restype = wintypes.BOOL
            handle = kernel32.OpenProcess(0x1000, False, pid)
            if not handle:
                return None
            try:
                created = wintypes.FILETIME()
                exited = wintypes.FILETIME()
                kernel = wintypes.FILETIME()
                user = wintypes.FILETIME()
                if not kernel32.GetProcessTimes(
                    handle,
                    ctypes.byref(created),
                    ctypes.byref(exited),
                    ctypes.byref(kernel),
                    ctypes.byref(user),
                ):
                    return None
                return str((created.dwHighDateTime << 32) | created.dwLowDateTime)
            finally:
                kernel32.CloseHandle(handle)
        except (AttributeError, OSError):
            return None

    def _owner_lock_held(self) -> bool:
        if fcntl is None:
            return False
        try:
            fd = os.open(self._owner_path, os.O_RDWR)
        except OSError:
            return False
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return True
            except OSError:
                return False
            fcntl.flock(fd, fcntl.LOCK_UN)
            return False
        finally:
            os.close(fd)

    def _record_has_held_owner(self, record: DaemonRecord) -> bool:
        if fcntl is None:
            owner = self._owner()
            return bool(
                owner
                and owner[0] == record.pid
                and record.identity
                and record.identity == self._pid_identity(record.pid)
            )
        for _ in range(3):
            owner = self._owner()
            held = self._owner_lock_held()
            if owner is not None and owner[0] == record.pid and held:
                return True
            if not held:
                return False
            time.sleep(0.01)
        return False

    def running_record(self) -> DaemonRecord | None:
        """Return the record only if its process is actually alive.

        Prunes a stale record (process gone) as a side effect so ``status``
        and ``start`` never act on a dead PID.
        """
        record = self.read_record()
        if record is None:
            return None
        if not self._pid_alive(record.pid):
            self._clear_record()
            return None
        return record

    def confirm_alive(self, record: DaemonRecord, *, grace_seconds: float) -> bool:
        """Return whether *record*'s process survives ``grace_seconds``.

        A detached daemon that dies on startup (e.g. missing config) leaves
        no signal on the terminal — the caller uses this to turn that silent
        failure into a visible error. Checks liveness immediately, then polls
        until the grace elapses; if the process is gone the stale record is
        pruned and ``False`` is returned.
        """
        deadline = time.time() + grace_seconds
        while True:
            if not self._pid_alive(record.pid):
                self._clear_record()
                return False
            if not self._record_has_held_owner(record):
                return False
            if time.time() >= deadline:
                return True
            time.sleep(0.1)

    def read_log_tail(self, max_lines: int = 20) -> str:
        """Return the last ``max_lines`` of the daemon's log (best-effort)."""
        record = self.read_record()
        if record is None:
            return ""
        try:
            lines = Path(record.log_path).read_text(errors="replace").splitlines()
        except OSError:
            return ""
        return "\n".join(lines[-max_lines:])

    # ── Lifecycle ─────────────────────────────────────────────────

    def start(
        self, argv: list[str], env: dict[str, str], *, cwd: Path | None = None
    ) -> DaemonRecord:
        """Spawn *argv* detached, record it, and return the record.

        Reuses the harness's detached-spawn kwargs (new session/process
        group) and combined-log capture, mirroring the host daemon. The
        caller is responsible for the already-running check.

        :param cwd: Working directory for the child. Set this when the
            integration resolves config from a CWD-relative path (e.g. a
            ``.env`` file) so the daemon doesn't inherit the arbitrary
            directory ``omni`` was launched from.
        """
        current = self.running_record()
        if current is not None:
            raise RuntimeError(f"{self.name} daemon already running (pid {current.pid})")
        self._acquire_owner()

        from omnigent.process_logging import (
            PROCESS_LOG_FILE_ENV_VAR,
            child_logging_popen_kwargs,
            open_process_log_file,
        )

        log_path, log_fh = open_process_log_file(self.name)
        env = {**env, PROCESS_LOG_FILE_ENV_VAR: str(log_path)}
        if self._owner_fd is not None:
            os.set_inheritable(self._owner_fd, True)
            env[self._owner_fd_env] = str(self._owner_fd)
            owner = self._owner()
            if owner is not None:
                env[self._owner_token_env] = owner[1]
        elif self._owner_token is not None:  # pragma: no cover - Windows fallback.
            env[self._owner_token_env] = self._owner_token
        # Detached: own session/process group (spawn_kwargs), stdin closed,
        # stdout+stderr to the log file. Mirrors the host daemon spawn.
        try:
            with child_logging_popen_kwargs(env) as logging_kwargs:
                if self._owner_fd is not None:
                    logging_kwargs["pass_fds"] = tuple(
                        {*logging_kwargs.get("pass_fds", ()), self._owner_fd}
                    )
                proc = subprocess.Popen(
                    argv,
                    env=env,
                    cwd=str(cwd) if cwd is not None else None,
                    stdin=subprocess.DEVNULL,
                    stdout=log_fh,
                    stderr=log_fh,
                    **_proc.spawn_kwargs(),
                    **logging_kwargs,
                )
        except BaseException:
            self._release_owner()
            raise
        finally:
            log_fh.close()
        record = DaemonRecord(
            pid=proc.pid,
            log_path=str(log_path),
            started_at=int(time.time()),
            identity=self._pid_identity(proc.pid),
        )
        if self._owner_fd is not None:
            fd, self._owner_fd = self._owner_fd, None
            try:
                self._write_owner(fd, proc.pid, env[self._owner_token_env])
            finally:
                os.close(fd)
        elif self._owner_token is not None:  # pragma: no cover - Windows fallback.
            self._owner_path.write_text(json.dumps({"pid": proc.pid, "token": self._owner_token}))
            self._owner_token = None
        self._write_record(record)
        return record

    def stop(self, *, grace_seconds: float = 5.0) -> DaemonRecord | None:
        """Terminate the running daemon; return the stopped record or ``None``.

        Sends SIGTERM to the daemon's process group (it was spawned in its
        own session), waits up to ``grace_seconds``, then escalates to
        SIGKILL. Idempotent: a missing/dead daemon clears the record and
        returns ``None``.
        """
        record = self.running_record()
        if record is None:
            self._clear_record()
            return None
        if not self._record_has_held_owner(record):
            raise RuntimeError(
                f"{self.name} daemon ownership cannot be verified; refusing to signal"
            )
        self._signal(record.pid, signal.SIGTERM)
        deadline = time.time() + grace_seconds
        while time.time() < deadline:
            if not self._pid_alive(record.pid):
                break
            time.sleep(0.1)
        if self._pid_alive(record.pid) and self._record_has_held_owner(record):
            self._signal(record.pid, getattr(signal, "SIGKILL", signal.SIGTERM))
        self._clear_record()
        self._release_owner(force=True)
        return record

    @staticmethod
    def _signal(pid: int, sig: int) -> None:
        """Signal the daemon's process group, falling back to the bare PID."""
        try:
            killpg = getattr(os, "killpg", None)
            if killpg is not None:
                killpg(os.getpgid(pid), sig)
            else:
                os.kill(pid, sig)
        except (ProcessLookupError, PermissionError, OSError):
            # Best-effort: fall back to a direct signal, ignore if already gone.
            with contextlib.suppress(OSError):
                os.kill(pid, sig)
