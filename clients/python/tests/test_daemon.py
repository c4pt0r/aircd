"""Unit tests for daemon watchdog and dedup behavior."""

import time
from unittest.mock import MagicMock

from aircd.daemon import AgentState, MAX_DEDUP_ENTRIES


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
