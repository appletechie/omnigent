"""Tests for ``omni integration slack`` and the integration daemon manager."""

from __future__ import annotations

import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest import mock

import pytest
from click.testing import CliRunner

from omnigent.cli import cli
from omnigent.integration_daemon import DaemonRecord, IntegrationDaemon


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX flock ownership test")
def test_stale_reused_pid_is_not_signaled_when_owner_lock_is_mismatched(tmp_path: Path) -> None:
    import fcntl
    import json
    import os

    daemon = IntegrationDaemon("slack", tmp_path)
    process = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"], start_new_session=True
    )
    daemon._write_record(DaemonRecord(pid=process.pid, log_path="/tmp/x.log", started_at=1))
    daemon._owner_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with daemon._owner_path.open("w+") as lock:
            json.dump({"pid": os.getpid(), "token": "other-owner"}, lock)
            lock.flush()
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)

            current = daemon.running_record()
            assert current is not None and current.pid == process.pid
            saved = daemon.read_record()
            assert saved is not None and saved.pid == process.pid
            with pytest.raises(RuntimeError, match="cannot be verified"):
                daemon.stop(grace_seconds=0)
            assert process.poll() is None
            assert json.loads(daemon._owner_path.read_text())["pid"] == os.getpid()
    finally:
        process.terminate()
        process.wait(timeout=5)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX flock ownership test")
def test_running_record_accepts_current_foreground_owner(tmp_path: Path) -> None:
    daemon = IntegrationDaemon("slack", tmp_path)
    record = daemon.acquire_current(tmp_path / "slack.log")
    try:
        assert daemon.running_record() == record
    finally:
        daemon.release_current()


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX flock ownership test")
def test_held_owner_lock_retries_transient_metadata_read(tmp_path: Path) -> None:
    import fcntl
    import json

    daemon = IntegrationDaemon("slack", tmp_path)
    record = DaemonRecord(pid=4242, log_path="/tmp/x.log", started_at=1)
    daemon._write_record(record)
    daemon._owner_path.parent.mkdir(parents=True, exist_ok=True)
    with daemon._owner_path.open("w+") as lock:
        json.dump({"pid": record.pid, "token": "owner"}, lock)
        lock.flush()
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        owner = daemon._owner()
        with (
            mock.patch.object(IntegrationDaemon, "_pid_alive", return_value=True),
            mock.patch.object(daemon, "_owner", side_effect=[None, owner]),
        ):
            assert daemon.running_record() == record
    assert daemon.read_record() == record


def test_live_legacy_record_blocks_duplicate_start_and_refuses_stop(tmp_path: Path) -> None:
    daemon = IntegrationDaemon("slack", tmp_path)
    process = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"], start_new_session=True
    )
    record = DaemonRecord(pid=process.pid, log_path="/tmp/x.log", started_at=1)
    daemon._write_record(record)
    try:
        assert daemon.running_record() == record
        with mock.patch("omnigent.integration_daemon.subprocess.Popen") as popen:
            with pytest.raises(RuntimeError, match="already running"):
                daemon.start(["never-run"], {})
        popen.assert_not_called()
        with pytest.raises(RuntimeError, match="cannot be verified"):
            daemon.stop(grace_seconds=0)
        assert process.poll() is None
        assert daemon.read_record() == record
    finally:
        process.terminate()
        process.wait(timeout=5)


def test_windows_owner_identity_allows_lifecycle_and_rejects_reuse(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import json

    import omnigent.integration_daemon as daemon_module

    daemon = IntegrationDaemon("slack", tmp_path)
    record = DaemonRecord(pid=4242, log_path="/tmp/x.log", started_at=1, identity="birth")
    daemon._write_record(record)
    daemon._owner_path.parent.mkdir(parents=True, exist_ok=True)
    daemon._owner_path.write_text(json.dumps({"pid": record.pid, "token": "owner"}))
    monkeypatch.setattr(daemon_module, "fcntl", None)
    with (
        mock.patch.object(IntegrationDaemon, "_pid_alive", return_value=True),
        mock.patch.object(IntegrationDaemon, "_pid_identity", return_value="birth"),
        mock.patch.object(IntegrationDaemon, "_signal"),
    ):
        assert daemon.running_record() == record
        assert daemon.confirm_alive(record, grace_seconds=0)
        assert daemon.stop(grace_seconds=0) == record

    daemon._write_record(record)
    daemon._owner_path.write_text(json.dumps({"pid": record.pid, "token": "owner"}))
    with (
        mock.patch.object(IntegrationDaemon, "_pid_alive", return_value=True),
        mock.patch.object(IntegrationDaemon, "_pid_identity", return_value="reused"),
        mock.patch.object(IntegrationDaemon, "_signal") as signal_process,
    ):
        assert daemon.running_record() == record
        with pytest.raises(RuntimeError, match="cannot be verified"):
            daemon.stop(grace_seconds=0)
    signal_process.assert_not_called()


def test_windows_pid_identity_uses_full_win32_process_times_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import ctypes
    from ctypes import wintypes
    from types import SimpleNamespace

    import omnigent.integration_daemon as daemon_module

    calls: dict[str, object] = {}

    class Function:
        def __init__(self, callback: object) -> None:
            self.callback = callback
            self.argtypes: object | None = None
            self.restype: object | None = None

        def __call__(self, *args: object) -> object:
            return self.callback(*args)  # type: ignore[operator]

    def open_process(access: object, inherit: object, pid: object) -> int:
        calls["open"] = (access, inherit, pid)
        return 1 << 40

    def get_times(handle: object, *times: object) -> int:
        calls["times"] = (handle, times)
        created = ctypes.cast(times[0], ctypes.POINTER(wintypes.FILETIME)).contents
        created.dwLowDateTime = 7
        created.dwHighDateTime = 3
        return 1

    def close_handle(handle: object) -> int:
        calls["close"] = handle
        return 1

    kernel32 = SimpleNamespace(
        OpenProcess=Function(open_process),
        GetProcessTimes=Function(get_times),
        CloseHandle=Function(close_handle),
    )
    monkeypatch.setattr(daemon_module.os, "name", "nt")
    monkeypatch.setattr(ctypes, "windll", SimpleNamespace(kernel32=kernel32), raising=False)

    assert IntegrationDaemon._pid_identity(42) == str((3 << 32) | 7)
    assert calls["open"] == (0x1000, False, 42)
    assert calls["close"] == 1 << 40
    handle, times = calls["times"]
    assert handle == 1 << 40
    assert len(times) == 4 and all(time is not None for time in times)
    assert kernel32.OpenProcess.argtypes == [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    assert kernel32.OpenProcess.restype is wintypes.HANDLE
    assert kernel32.GetProcessTimes.argtypes == [
        wintypes.HANDLE,
        ctypes.POINTER(wintypes.FILETIME),
        ctypes.POINTER(wintypes.FILETIME),
        ctypes.POINTER(wintypes.FILETIME),
        ctypes.POINTER(wintypes.FILETIME),
    ]
    assert kernel32.GetProcessTimes.restype is wintypes.BOOL
    assert kernel32.CloseHandle.argtypes == [wintypes.HANDLE]
    assert kernel32.CloseHandle.restype is wintypes.BOOL


def test_windows_pid_alive_never_uses_os_kill(monkeypatch: pytest.MonkeyPatch) -> None:
    import omnigent.integration_daemon as daemon_module

    monkeypatch.setattr(daemon_module.os, "name", "nt")
    with (
        mock.patch.object(IntegrationDaemon, "_pid_identity", return_value="birth"),
        mock.patch.object(daemon_module.os, "kill") as kill,
    ):
        assert IntegrationDaemon._pid_alive(42)
    kill.assert_not_called()


@pytest.fixture
def data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Isolate daemon state under a temp OMNIGENT_DATA_DIR."""
    monkeypatch.setenv("OMNIGENT_DATA_DIR", str(tmp_path))
    return tmp_path


# ── IntegrationDaemon (unit) ──────────────────────────────────────


def test_record_round_trip_and_prune(tmp_path: Path) -> None:
    d = IntegrationDaemon("slack", tmp_path)
    assert d.read_record() is None
    d._write_record(DaemonRecord(pid=4242, log_path="/tmp/x.log", started_at=1))
    rec = d.read_record()
    assert rec is not None and rec.pid == 4242 and rec.log_path == "/tmp/x.log"

    # A dead PID is pruned by running_record().
    with mock.patch.object(IntegrationDaemon, "_pid_alive", return_value=False):
        assert d.running_record() is None
    assert d.read_record() is None  # pruned from disk


def test_start_writes_record_detached(tmp_path: Path) -> None:
    d = IntegrationDaemon("slack", tmp_path)
    with mock.patch("omnigent.integration_daemon.subprocess.Popen") as popen:
        popen.return_value.pid = 777
        record = d.start(["python", "-m", "omnigent_slack"], {"A": "b"})
    assert record.pid == 777
    assert d.read_record() == record
    # Spawned detached (own session/process group) with stdin closed.
    _, kwargs = popen.call_args
    assert kwargs["stdin"] == subprocess.DEVNULL
    assert "start_new_session" in kwargs or "creationflags" in kwargs


def test_stop_signals_and_clears(tmp_path: Path) -> None:
    d = IntegrationDaemon("slack", tmp_path)
    d._write_record(DaemonRecord(pid=4242, log_path="/tmp/x.log", started_at=1))
    calls: list[int] = []
    # Alive for the first liveness check (running_record), then dead so stop()
    # doesn't spin.
    alive = iter([True, False, False])
    with (
        mock.patch.object(IntegrationDaemon, "_pid_alive", side_effect=lambda _pid: next(alive)),
        mock.patch.object(IntegrationDaemon, "_record_has_held_owner", return_value=True),
        mock.patch.object(
            IntegrationDaemon, "_signal", side_effect=lambda pid, sig: calls.append(sig)
        ),
    ):
        stopped = d.stop(grace_seconds=0.0)
    assert stopped is not None and stopped.pid == 4242
    assert calls  # at least a SIGTERM was sent
    assert d.read_record() is None


def test_stop_when_not_running_is_noop(tmp_path: Path) -> None:
    d = IntegrationDaemon("slack", tmp_path)
    assert d.stop() is None


def test_confirm_alive_prunes_dead_record(tmp_path: Path) -> None:
    d = IntegrationDaemon("slack", tmp_path)
    record = DaemonRecord(pid=4242, log_path="/tmp/x.log", started_at=1)
    d._write_record(record)
    with mock.patch.object(IntegrationDaemon, "_pid_alive", return_value=False):
        assert d.confirm_alive(record, grace_seconds=0.0) is False
    assert d.read_record() is None  # pruned
    with (
        mock.patch.object(IntegrationDaemon, "_pid_alive", return_value=True),
        mock.patch.object(IntegrationDaemon, "_record_has_held_owner", return_value=True),
    ):
        d._write_record(record)
        assert d.confirm_alive(record, grace_seconds=0.0) is True


def test_concurrent_daemon_claims_have_exactly_one_owner(tmp_path: Path) -> None:
    daemons = [IntegrationDaemon("polly", tmp_path), IntegrationDaemon("polly", tmp_path)]
    barrier = threading.Barrier(2)

    def synchronized_missing_record(_daemon: IntegrationDaemon) -> None:
        barrier.wait()

    def claim(daemon: IntegrationDaemon) -> bool:
        try:
            daemon.acquire_current(tmp_path / "polly.log")
        except RuntimeError:
            return False
        return True

    with mock.patch.object(IntegrationDaemon, "running_record", synchronized_missing_record):
        with ThreadPoolExecutor(max_workers=2) as pool:
            claimed = list(pool.map(claim, daemons))

    assert sorted(claimed) == [False, True]
    daemons[claimed.index(True)].release_current()


def test_background_owner_lock_survives_spawn_handoff(tmp_path: Path) -> None:
    ready = tmp_path / "ready"
    script = """
import sys
import time
from pathlib import Path
from omnigent.integration_daemon import IntegrationDaemon

daemon = IntegrationDaemon("polly", Path(sys.argv[1]))
daemon.acquire_current(Path(sys.argv[1]) / "polly.log")
Path(sys.argv[2]).write_text("ready")
try:
    time.sleep(30)
finally:
    daemon.release_current()
"""
    daemon = IntegrationDaemon("polly", tmp_path)
    record = daemon.start([sys.executable, "-c", script, str(tmp_path), str(ready)], {})
    try:
        deadline = time.time() + 5
        while not ready.exists() and time.time() < deadline:
            time.sleep(0.05)
        assert ready.exists()
        assert daemon.running_record() == record

        daemon._clear_record()
        with pytest.raises(RuntimeError, match="already running"):
            IntegrationDaemon("polly", tmp_path).acquire_current(tmp_path / "other.log")
    finally:
        daemon._write_record(record)
        daemon.stop(grace_seconds=1)


# ── CLI wiring ────────────────────────────────────────────────────


def test_slack_background_hint_when_not_installed(data_dir: Path) -> None:
    runner = CliRunner()
    with mock.patch("omnigent.cli._slack_installed", return_value=False):
        result = runner.invoke(cli, ["integration", "slack", "--background"])
    assert result.exit_code != 0
    assert "isn't installed" in result.output
    assert "omnigent-slack" in result.output


def test_slack_foreground_hint_when_not_installed(data_dir: Path) -> None:
    runner = CliRunner()
    with mock.patch("omnigent.cli._slack_installed", return_value=False):
        result = runner.invoke(cli, ["integration", "slack"])
    assert result.exit_code != 0
    assert "isn't installed" in result.output


def test_slack_status_reports_not_running(data_dir: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(cli, ["integration", "slack", "status"])
    assert result.exit_code == 0
    assert "not running" in result.output


def test_slack_background_status_stop_lifecycle(data_dir: Path) -> None:
    runner = CliRunner()
    with (
        mock.patch("omnigent.cli._slack_installed", return_value=True),
        mock.patch("omnigent.integration_daemon.subprocess.Popen") as popen,
        # The spawned pid is a mock, not a real process — force liveness true so
        # status reports running. confirm_alive (startup-crash detection) has
        # its own tests; short-circuit it here so the happy path doesn't wait
        # out the grace period.
        mock.patch.object(IntegrationDaemon, "_pid_alive", return_value=True),
        mock.patch.object(IntegrationDaemon, "_record_has_held_owner", return_value=True),
        mock.patch.object(IntegrationDaemon, "confirm_alive", return_value=True),
    ):
        popen.return_value.pid = 9911
        start = runner.invoke(cli, ["integration", "slack", "--background"])
        assert start.exit_code == 0, start.output
        assert "9911" in start.output
        # Argv targets the slack package in the current interpreter.
        argv = popen.call_args.args[0]
        assert argv[1:] == ["-m", "omnigent_slack"]

        status = runner.invoke(cli, ["integration", "slack", "status"])
        assert "running" in status.output and "9911" in status.output

        # --background again is idempotent — reports the existing pid, no 2nd spawn.
        popen.reset_mock()
        again = runner.invoke(cli, ["integration", "slack", "--background"])
        assert "already running" in again.output
        popen.assert_not_called()

    # stop terminates and clears.
    with (
        mock.patch.object(IntegrationDaemon, "_pid_alive", side_effect=[True, False, False]),
        mock.patch.object(IntegrationDaemon, "_record_has_held_owner", return_value=True),
        mock.patch.object(IntegrationDaemon, "_signal"),
    ):
        stop = runner.invoke(cli, ["integration", "slack", "stop"])
        assert stop.exit_code == 0
        assert "Stopped" in stop.output

    # After stop, status is clean again.
    assert "not running" in runner.invoke(cli, ["integration", "slack", "status"]).output


def test_slack_background_reports_immediate_exit(data_dir: Path) -> None:
    """A daemon that dies on startup fails loudly with a log tail, not a lie."""
    runner = CliRunner()
    with (
        mock.patch("omnigent.cli._slack_installed", return_value=True),
        mock.patch("omnigent.integration_daemon.subprocess.Popen") as popen,
        # Process is gone by the time confirm_alive checks.
        mock.patch.object(IntegrationDaemon, "_pid_alive", return_value=False),
        mock.patch.object(IntegrationDaemon, "read_log_tail", return_value="Traceback: boom"),
    ):
        popen.return_value.pid = 5150
        result = runner.invoke(cli, ["integration", "slack", "--background"])
    assert result.exit_code != 0
    assert "exited immediately" in result.output
    assert "boom" in result.output
    # The dead record was pruned, so status is clean.
    assert "not running" in runner.invoke(cli, ["integration", "slack", "status"]).output


def test_slack_foreground_runs_subprocess(data_dir: Path) -> None:
    runner = CliRunner()
    with (
        mock.patch("omnigent.cli._slack_installed", return_value=True),
        mock.patch("omnigent.cli.subprocess.run") as run,
    ):
        run.return_value = mock.Mock(returncode=0)
        result = runner.invoke(cli, ["integration", "slack"])
    assert result.exit_code == 0
    argv = run.call_args.args[0]
    assert argv[1:] == ["-m", "omnigent_slack"]


def test_slack_start_subcommand_removed(data_dir: Path) -> None:
    """The old ``start`` subcommand is gone — ``--background`` replaces it.

    Guards the migration: a lingering ``start`` would be treated by Click as an
    unknown subcommand (usage error), so this asserts the flag is the only way
    in and the subcommand isn't silently re-added."""
    runner = CliRunner()
    result = runner.invoke(cli, ["integration", "slack", "start"])
    assert result.exit_code != 0
    assert "No such command 'start'" in result.output


def test_slack_foreground_refuses_when_daemon_running(data_dir: Path) -> None:
    """Bare (foreground) run refuses if a background daemon holds the socket."""
    IntegrationDaemon("slack", data_dir)._write_record(
        DaemonRecord(pid=4242, log_path="/tmp/x.log", started_at=1)
    )
    runner = CliRunner()
    with (
        mock.patch("omnigent.cli._slack_installed", return_value=True),
        mock.patch.object(IntegrationDaemon, "_pid_alive", return_value=True),
        mock.patch.object(IntegrationDaemon, "_record_has_held_owner", return_value=True),
        mock.patch("omnigent.cli.subprocess.run") as run,
    ):
        result = runner.invoke(cli, ["integration", "slack"])
    assert result.exit_code != 0
    assert "already running" in result.output
    run.assert_not_called()  # never spawned a second bot


def test_slack_logs_prints_path(data_dir: Path) -> None:
    runner = CliRunner()
    # No daemon yet.
    none = runner.invoke(cli, ["integration", "slack", "logs"])
    assert "No Slack daemon" in none.output
    # With a record, prints the path.
    IntegrationDaemon("slack", data_dir)._write_record(
        DaemonRecord(pid=1, log_path="/tmp/slack.log", started_at=1)
    )
    result = runner.invoke(cli, ["integration", "slack", "logs"])
    assert result.exit_code == 0
    assert "/tmp/slack.log" in result.output


def test_integration_group_bare_shows_help(data_dir: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(cli, ["integration"])
    assert result.exit_code == 0
    assert "slack" in result.output.lower()
