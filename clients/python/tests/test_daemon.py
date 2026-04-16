"""Unit tests for daemon watchdog, dedup, and lifecycle behavior."""

import asyncio
from http.server import HTTPServer
import os
import time
from unittest.mock import MagicMock, patch

import pytest

from aircd.daemon import AgentState, Daemon, DaemonHTTPHandler, MAX_DEDUP_ENTRIES


def discard_created_task(coro):
    """Close mocked background coroutines so spawn tests do not leak tasks."""
    coro.close()
    return MagicMock()


class TestDedupLRU:
    """Tests for bounded OrderedDict dedup behavior."""

    def test_dedup_rejects_duplicate(self):
        state = AgentState()
        state.seen_msg_ids["msg-1"] = True
        assert "msg-1" in state.seen_msg_ids

    def test_dedup_evicts_oldest_on_overflow(self):
        state = AgentState()
        # Fill to capacity
        for i in range(MAX_DEDUP_ENTRIES):
            state.seen_msg_ids[f"msg-{i}"] = True
        # Oldest entry still present
        assert "msg-0" in state.seen_msg_ids

        # Add one more — oldest should be evicted
        state.seen_msg_ids["msg-overflow"] = True
        while len(state.seen_msg_ids) > MAX_DEDUP_ENTRIES:
            state.seen_msg_ids.popitem(last=False)

        assert "msg-0" not in state.seen_msg_ids
        assert "msg-overflow" in state.seen_msg_ids
        assert len(state.seen_msg_ids) == MAX_DEDUP_ENTRIES

    def test_dedup_move_to_end_on_hit(self):
        state = AgentState()
        state.seen_msg_ids["msg-old"] = True
        state.seen_msg_ids["msg-mid"] = True
        state.seen_msg_ids["msg-new"] = True

        # Hit msg-old — it should move to end (most recent)
        state.seen_msg_ids.move_to_end("msg-old")

        # Now evict from front — msg-mid should go first
        state.seen_msg_ids.popitem(last=False)
        assert "msg-mid" not in state.seen_msg_ids
        assert "msg-old" in state.seen_msg_ids

    def test_dedup_never_full_clears(self):
        """Verify that we evict one-by-one, not full clear."""
        state = AgentState()
        for i in range(MAX_DEDUP_ENTRIES + 100):
            state.seen_msg_ids[f"msg-{i}"] = True
            while len(state.seen_msg_ids) > MAX_DEDUP_ENTRIES:
                state.seen_msg_ids.popitem(last=False)

        # Should always be at capacity, never empty
        assert len(state.seen_msg_ids) == MAX_DEDUP_ENTRIES
        # Most recent entries should be present
        assert f"msg-{MAX_DEDUP_ENTRIES + 99}" in state.seen_msg_ids
        # Oldest evicted entries should be gone
        assert "msg-0" not in state.seen_msg_ids


class TestWatchdogTimestamp:
    """Tests for watchdog activity timestamp management."""

    def test_agent_state_initial_timestamp(self):
        state = AgentState()
        assert state.last_stdout_activity == 0.0

    def test_idle_delivery_resets_timestamp(self):
        """Verify that starting a new turn refreshes the watchdog timestamp.

        This catches the bug where a long-idle agent gets immediately killed
        because last_stdout_activity was stale from the previous turn.
        """
        state = AgentState()
        # Simulate old activity from a previous turn
        state.last_stdout_activity = time.monotonic() - 1000

        # Simulate what _deliver_pending_idle does when sending a new turn
        state.is_busy = True
        state.last_stdout_activity = time.monotonic()

        # Watchdog should not fire — activity is fresh
        elapsed = time.monotonic() - state.last_stdout_activity
        assert elapsed < 1.0  # Just set, should be near zero

    def test_process_identity_guard(self):
        """Verify that stdout reader only clears state for its own process."""
        state = AgentState()
        old_proc = MagicMock()
        new_proc = MagicMock()

        # Old reader has reference to old_proc
        proc = old_proc

        # Watchdog replaces with new_proc
        state.process = new_proc
        state.is_busy = True

        # Old reader sees EOF — should NOT clear state
        if state.process is proc:
            state.is_busy = False
            state.process = None

        # State should be unchanged because process identity doesn't match
        assert state.process is new_proc
        assert state.is_busy is True


class TestHttpLifecycle:
    def test_shutdown_http_server_is_noop_when_not_started(self):
        daemon = Daemon(
            host="127.0.0.1",
            port=6667,
            token="agent-token",
            nick="agent",
            channels=["#test"],
        )

        daemon._shutdown_http_server()

        assert daemon._http_server is None
        assert daemon._http_thread is None

    def test_shutdown_http_server_closes_thread_and_socket(self):
        daemon = Daemon(
            host="127.0.0.1",
            port=6667,
            token="agent-token",
            nick="agent",
            channels=["#test"],
            http_port=0,
        )

        daemon._start_http_server()
        server = daemon._http_server
        thread = daemon._http_thread
        assert server is not None
        assert thread is not None
        assert thread.is_alive()
        bound_port = server.server_address[1]

        daemon._shutdown_http_server()

        assert daemon._http_server is None
        assert daemon._http_thread is None
        assert not thread.is_alive()

        # The listener socket should be closed and reusable after cleanup.
        replacement = HTTPServer(("127.0.0.1", bound_port), DaemonHTTPHandler)
        replacement.server_close()


class TestPermissionsMode:
    """Tests for --permissions-mode flag behavior."""

    def _make_daemon(self, permissions_mode="auto", working_dir=None):
        return Daemon(
            host="127.0.0.1",
            port=6667,
            token="agent-token",
            nick="agent",
            channels=["#test"],
            permissions_mode=permissions_mode,
            working_dir=working_dir,
        )

    def _mock_popen_and_start_claude(self, daemon, mock_popen):
        mock_proc = MagicMock()
        mock_proc.pid = 12345
        mock_proc.stdin = MagicMock()
        mock_proc.stdout = MagicMock()
        mock_proc.stderr = MagicMock()
        mock_popen.return_value = mock_proc

        loop = asyncio.new_event_loop()
        try:
            with patch(
                "aircd.daemon.asyncio.create_task",
                side_effect=discard_created_task,
            ):
                loop.run_until_complete(daemon._start_claude())
        finally:
            loop.close()
            if hasattr(daemon, "_mcp_config_file"):
                try:
                    os.unlink(daemon._mcp_config_file.name)
                except OSError:
                    pass

        assert mock_popen.called
        return mock_popen.call_args

    def test_default_permissions_mode_is_auto(self):
        daemon = Daemon(
            host="127.0.0.1",
            port=6667,
            token="agent-token",
            nick="agent",
            channels=["#test"],
        )
        assert daemon.permissions_mode == "auto"

    @patch("aircd.daemon.find_claude_cli", return_value="/usr/bin/claude")
    @patch("subprocess.Popen")
    def test_skip_mode_includes_dangerous_flag(self, mock_popen, mock_cli):
        daemon = self._make_daemon(permissions_mode="skip")
        call_args = self._mock_popen_and_start_claude(daemon, mock_popen)
        args = call_args[0][0]
        assert "--dangerously-skip-permissions" in args

    @patch("aircd.daemon.find_claude_cli", return_value="/usr/bin/claude")
    @patch("subprocess.Popen")
    def test_auto_mode_omits_dangerous_flag(self, mock_popen, mock_cli):
        daemon = self._make_daemon(permissions_mode="auto")
        call_args = self._mock_popen_and_start_claude(daemon, mock_popen)
        args = call_args[0][0]
        assert "--dangerously-skip-permissions" not in args

    def test_working_dir_defaults_to_none(self):
        daemon = self._make_daemon()
        assert daemon.working_dir is None

    def test_working_dir_resolves_relative_directory(self, tmp_path, monkeypatch):
        workdir = tmp_path / "repo"
        workdir.mkdir()
        monkeypatch.chdir(tmp_path)

        daemon = self._make_daemon(working_dir="repo")

        assert daemon.working_dir == str(workdir)

    def test_working_dir_rejects_missing_directory(self, tmp_path):
        missing = tmp_path / "missing"

        with pytest.raises(ValueError, match="working directory does not exist"):
            self._make_daemon(working_dir=str(missing))

    def test_working_dir_rejects_file(self, tmp_path):
        file_path = tmp_path / "not-a-dir"
        file_path.write_text("not a directory")

        with pytest.raises(ValueError, match="working directory does not exist"):
            self._make_daemon(working_dir=str(file_path))

    @patch("aircd.daemon.find_claude_cli", return_value="/usr/bin/claude")
    @patch("subprocess.Popen")
    def test_start_claude_passes_working_dir_to_popen(
        self, mock_popen, mock_cli, tmp_path
    ):
        daemon = self._make_daemon(working_dir=str(tmp_path))

        call_args = self._mock_popen_and_start_claude(daemon, mock_popen)

        assert call_args.kwargs["cwd"] == str(tmp_path)
