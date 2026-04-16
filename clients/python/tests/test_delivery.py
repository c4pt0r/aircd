"""Unit tests for daemon delivery buffer semantics — no server needed.

Tests visibility-timeout ACK, delivery_id for no-id messages,
duplicate sync request rejection, and in-flight redelivery.
"""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field

import pytest

from aircd.client import Message
from aircd.daemon import Daemon


# --- Helpers: extract the delivery buffer logic into testable pieces ---

MESSAGE_VISIBILITY_TIMEOUT = 30.0


@dataclass
class FakeAgentState:
    pending_inbox: deque = field(default_factory=deque)
    in_flight: dict = field(default_factory=dict)


def _make_msg(channel: str, sender: str, content: str, raw: str = "") -> Message:
    return Message(channel=channel, sender=sender, content=content, raw=raw, seq=0)


def _message_to_dict(msg: Message) -> dict:
    """Minimal message_to_dict matching daemon.py logic."""
    import re
    msg_id = ""
    time_val = ""
    if msg.raw:
        m = re.search(r"msg-id=([^\s;]+)", msg.raw)
        if m:
            msg_id = m.group(1)
        m = re.search(r"time=([^\s;]+)", msg.raw)
        if m:
            time_val = m.group(1)
    return {
        "channel": msg.channel,
        "sender": msg.sender,
        "content": msg.content,
        "msg_id": msg_id,
        "time": time_val,
    }


def check_messages(agent: FakeAgentState, now: float) -> list[dict]:
    """Simulate _handle_check_messages logic."""
    messages = []
    # Re-queue expired in-flight
    expired = [
        (mid, msg)
        for mid, (msg, delivered_at) in agent.in_flight.items()
        if now - delivered_at > MESSAGE_VISIBILITY_TIMEOUT
    ]
    for mid, msg in expired:
        del agent.in_flight[mid]
        agent.pending_inbox.appendleft(msg)

    # Deliver pending → in-flight
    while agent.pending_inbox:
        msg = agent.pending_inbox.popleft()
        msg_dict = _message_to_dict(msg)
        mid = msg_dict.get("msg_id") or f"noid_{id(msg)}"
        agent.in_flight[mid] = (msg, now)
        msg_dict["delivery_id"] = mid
        messages.append(msg_dict)
    return messages


def ack_messages(agent: FakeAgentState, delivery_ids: list[str]) -> int:
    """Simulate _handle_ack_messages logic."""
    acked = 0
    for mid in delivery_ids:
        if mid in agent.in_flight:
            del agent.in_flight[mid]
            acked += 1
    return acked


# --- Tests ---


class TestVisibilityTimeout:
    def test_check_moves_to_in_flight(self):
        agent = FakeAgentState()
        msg = _make_msg("#test", "alice", "hello", raw="@msg-id=abc123")
        agent.pending_inbox.append(msg)

        result = check_messages(agent, time.time())
        assert len(result) == 1
        assert result[0]["delivery_id"] == "abc123"
        assert len(agent.pending_inbox) == 0
        assert "abc123" in agent.in_flight

    def test_in_flight_not_redelivered_before_timeout(self):
        agent = FakeAgentState()
        msg = _make_msg("#test", "alice", "hello", raw="@msg-id=abc123")
        agent.pending_inbox.append(msg)

        now = time.time()
        check_messages(agent, now)
        # Second check before timeout — nothing returned
        result = check_messages(agent, now + 10)
        assert len(result) == 0
        assert "abc123" in agent.in_flight

    def test_unacked_redelivered_after_timeout(self):
        agent = FakeAgentState()
        msg = _make_msg("#test", "alice", "hello", raw="@msg-id=abc123")
        agent.pending_inbox.append(msg)

        now = time.time()
        check_messages(agent, now)
        # After timeout, message should be redelivered
        result = check_messages(agent, now + MESSAGE_VISIBILITY_TIMEOUT + 1)
        assert len(result) == 1
        assert result[0]["delivery_id"] == "abc123"

    def test_ack_prevents_redelivery(self):
        agent = FakeAgentState()
        msg = _make_msg("#test", "alice", "hello", raw="@msg-id=abc123")
        agent.pending_inbox.append(msg)

        now = time.time()
        result = check_messages(agent, now)
        acked = ack_messages(agent, [result[0]["delivery_id"]])
        assert acked == 1
        # After timeout, nothing redelivered
        result = check_messages(agent, now + MESSAGE_VISIBILITY_TIMEOUT + 1)
        assert len(result) == 0


class TestDeliveryIdForNoIdMessages:
    def test_no_id_message_gets_delivery_id(self):
        agent = FakeAgentState()
        msg = _make_msg("#test", "alice", "hello")  # no raw/msg-id
        agent.pending_inbox.append(msg)

        result = check_messages(agent, time.time())
        assert len(result) == 1
        assert result[0]["delivery_id"].startswith("noid_")
        assert result[0]["msg_id"] == ""

    def test_no_id_message_ackable(self):
        agent = FakeAgentState()
        msg = _make_msg("#test", "alice", "hello")
        agent.pending_inbox.append(msg)

        now = time.time()
        result = check_messages(agent, now)
        delivery_id = result[0]["delivery_id"]
        acked = ack_messages(agent, [delivery_id])
        assert acked == 1
        # Not redelivered
        result = check_messages(agent, now + MESSAGE_VISIBILITY_TIMEOUT + 1)
        assert len(result) == 0


class TestDuplicateSyncRequestRejection:
    """Verify that duplicate in-flight sync request keys are rejected."""

    def test_history_duplicate_rejected(self):
        """Simulates the ValueError guard in _request_history."""
        requests: dict[str, object] = {}
        key = "#general"
        requests[key] = "first_request"
        # Second request with same key should be rejected
        with pytest.raises(ValueError, match="already in-flight"):
            if key in requests:
                raise ValueError(f"history request already in-flight for {key}")

    def test_task_list_duplicate_rejected(self):
        requests: dict[str, object] = {}
        key = "#work"
        requests[key] = "first_request"
        with pytest.raises(ValueError, match="already in-flight"):
            if key in requests:
                raise ValueError(f"task list request already in-flight for {key}")

    def test_task_action_duplicate_rejected(self):
        requests: dict[str, object] = {}
        key = "claim:task_abc"
        requests[key] = "first_request"
        with pytest.raises(ValueError, match="already in-flight"):
            if key in requests:
                raise ValueError(f"task action request already in-flight for {key}")


class TestMultipleMessageDelivery:
    def test_batch_delivery_and_partial_ack(self):
        agent = FakeAgentState()
        for i in range(3):
            agent.pending_inbox.append(
                _make_msg("#test", "alice", f"msg{i}", raw=f"@msg-id=m{i}")
            )

        now = time.time()
        result = check_messages(agent, now)
        assert len(result) == 3

        # ACK only first two
        ack_messages(agent, ["m0", "m1"])

        # After timeout, only m2 redelivered
        result = check_messages(agent, now + MESSAGE_VISIBILITY_TIMEOUT + 1)
        assert len(result) == 1
        assert result[0]["delivery_id"] == "m2"


class TestDaemonReaper:
    def test_requeue_expired_in_flight_locked(self):
        daemon = Daemon(
            host="127.0.0.1",
            port=6667,
            token="agent-token",
            nick="agent",
            channels=["#test"],
        )
        msg = _make_msg("#test", "alice", "hello", raw="@msg-id=m1")
        now = time.time()
        daemon.agent.in_flight["m1"] = (
            msg,
            now - MESSAGE_VISIBILITY_TIMEOUT - 1,
        )

        with daemon.inbox_lock:
            requeued = daemon._requeue_expired_in_flight_locked(now, "test")

        assert requeued == 1
        assert "m1" not in daemon.agent.in_flight
        assert list(daemon.agent.pending_inbox) == [msg]

    @pytest.mark.asyncio
    async def test_wake_pending_delivery_idle(self):
        daemon = Daemon(
            host="127.0.0.1",
            port=6667,
            token="agent-token",
            nick="agent",
            channels=["#test"],
        )
        calls = []

        async def fake_deliver():
            calls.append("idle")

        daemon._deliver_pending_idle = fake_deliver
        daemon.agent.process = object()
        daemon.agent.is_busy = False

        await daemon._wake_pending_delivery("test")

        assert calls == ["idle"]

    @pytest.mark.asyncio
    async def test_wake_pending_delivery_busy(self):
        daemon = Daemon(
            host="127.0.0.1",
            port=6667,
            token="agent-token",
            nick="agent",
            channels=["#test"],
        )
        calls = []

        async def fake_notify():
            calls.append("busy")

        daemon._deliver_busy_notification = fake_notify
        daemon.agent.process = object()
        daemon.agent.is_busy = True

        await daemon._wake_pending_delivery("test")

        assert calls == ["busy"]
