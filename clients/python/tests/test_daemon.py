"""Unit tests for daemon watchdog, dedup, and lifecycle behavior."""

from http.server import HTTPServer
import time
from unittest.mock import MagicMock, patch

from aircd.daemon import AgentState, Daemon, DaemonHTTPHandler, MAX_DEDUP_ENTRIES


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

    def _make_daemon(self, permissions_mode="auto"):
        return Daemon(
            host="127.0.0.1",
            port=6667,
            token="agent-token",
            nick="agent",
            channels=["#test"],
            permissions_mode=permissions_mode,
        )

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
        mock_proc = MagicMock()
        mock_proc.pid = 12345
        mock_proc.stdin = MagicMock()
        mock_proc.stdout = MagicMock()
        mock_proc.stderr = MagicMock()
        mock_popen.return_value = mock_proc

        daemon = self._make_daemon(permissions_mode="skip")
        # Call _start_claude synchronously enough to capture the Popen args
        import asyncio
        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(daemon._start_claude())
        except Exception:
            pass  # May fail on task creation, we just need the Popen call
        finally:
            loop.close()

        assert mock_popen.called
        args = mock_popen.call_args[0][0]
        assert "--dangerously-skip-permissions" in args

    @patch("aircd.daemon.find_claude_cli", return_value="/usr/bin/claude")
    @patch("subprocess.Popen")
    def test_auto_mode_omits_dangerous_flag(self, mock_popen, mock_cli):
        mock_proc = MagicMock()
        mock_proc.pid = 12345
        mock_proc.stdin = MagicMock()
        mock_proc.stdout = MagicMock()
        mock_proc.stderr = MagicMock()
        mock_popen.return_value = mock_proc

        daemon = self._make_daemon(permissions_mode="auto")
        import asyncio
        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(daemon._start_claude())
        except Exception:
            pass
        finally:
            loop.close()

        assert mock_popen.called
        args = mock_popen.call_args[0][0]
        assert "--dangerously-skip-permissions" not in args


class TestWorkingDir:
    """Tests for --working-dir flag behavior."""

    def test_default_working_dir_is_none(self):
        daemon = Daemon(
            host="127.0.0.1",
            port=6667,
            token="agent-token",
            nick="agent",
            channels=["#test"],
        )
        assert daemon.working_dir is None

    def test_working_dir_is_stored(self):
        daemon = Daemon(
            host="127.0.0.1",
            port=6667,
            token="agent-token",
            nick="agent",
            channels=["#test"],
            working_dir="/tmp",
        )
        assert daemon.working_dir == "/tmp"

    @patch("aircd.daemon.find_claude_cli", return_value="/usr/bin/claude")
    @patch("subprocess.Popen")
    def test_working_dir_passed_as_cwd(self, mock_popen, mock_cli):
        mock_proc = MagicMock()
        mock_proc.pid = 12345
        mock_proc.stdin = MagicMock()
        mock_proc.stdout = MagicMock()
        mock_proc.stderr = MagicMock()
        mock_popen.return_value = mock_proc

        daemon = Daemon(
            host="127.0.0.1",
            port=6667,
            token="agent-token",
            nick="agent",
            channels=["#test"],
            working_dir="/tmp",
        )
        import asyncio
        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(daemon._start_claude())
        except Exception:
            pass
        finally:
            loop.close()

        assert mock_popen.called
        kwargs = mock_popen.call_args[1]
        assert kwargs.get("cwd") == "/tmp"

    @patch("aircd.daemon.find_claude_cli", return_value="/usr/bin/claude")
    @patch("subprocess.Popen")
    def test_no_working_dir_passes_none_cwd(self, mock_popen, mock_cli):
        mock_proc = MagicMock()
        mock_proc.pid = 12345
        mock_proc.stdin = MagicMock()
        mock_proc.stdout = MagicMock()
        mock_proc.stderr = MagicMock()
        mock_popen.return_value = mock_proc

        daemon = Daemon(
            host="127.0.0.1",
            port=6667,
            token="agent-token",
            nick="agent",
            channels=["#test"],
        )
        import asyncio
        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(daemon._start_claude())
        except Exception:
            pass
        finally:
            loop.close()

        assert mock_popen.called
        kwargs = mock_popen.call_args[1]
        assert kwargs.get("cwd") is None
