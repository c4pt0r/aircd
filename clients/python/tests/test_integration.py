"""Integration tests for aircd server.

These tests require a running aircd server. Set AIRCD_HOST and AIRCD_PORT
environment variables, or defaults to localhost:6667.

The server must have these test tokens pre-configured in its principals table:
  - token "test-token-1" -> principal with nick "agent-1"
  - token "test-token-2" -> principal with nick "agent-2"
  - token "test-token-3" -> principal with nick "agent-3"

Run: pytest tests/test_integration.py -v
"""

from __future__ import annotations

import asyncio
import os
import re

import pytest

from aircd.client import AircdClient

AIRCD_HOST = os.environ.get("AIRCD_HOST", "localhost")
AIRCD_PORT = int(os.environ.get("AIRCD_PORT", "6667"))

pytestmark = pytest.mark.asyncio


async def _make_client(token: str, nick: str) -> AircdClient:
    client = AircdClient(
        AIRCD_HOST, AIRCD_PORT, token=token, nick=nick, auto_reconnect=False
    )
    await client.connect()
    return client


def _server_reachable() -> bool:
    import socket

    try:
        s = socket.create_connection((AIRCD_HOST, AIRCD_PORT), timeout=2)
        s.close()
        return True
    except OSError:
        return False


skip_no_server = pytest.mark.skipif(
    not _server_reachable(),
    reason=f"aircd server not reachable at {AIRCD_HOST}:{AIRCD_PORT}",
)


# ── Basic connectivity ──────────────────────────────────────────


@skip_no_server
async def test_connect_and_join():
    """Agent can connect, authenticate, and join a channel."""
    client = await _make_client("test-token-1", "agent-1")
    try:
        await client.join("#test-basic")
        assert client._state.registered
        assert "#test-basic" in client._state.channels
    finally:
        await client.close()


@skip_no_server
async def test_two_agents_see_each_other():
    """Two agents in the same channel can exchange messages."""
    c1 = await _make_client("test-token-1", "agent-1")
    c2 = await _make_client("test-token-2", "agent-2")
    try:
        await c1.join("#test-chat")
        await c2.join("#test-chat")
        await asyncio.sleep(0.2)

        await c1.privmsg("#test-chat", "hello from agent-1")

        received = False
        async for msg in c2.messages():
            if msg.sender == "agent-1" and "hello from agent-1" in msg.content:
                received = True
                break
        assert received, "agent-2 did not receive message from agent-1"
    finally:
        await c1.close()
        await c2.close()


@skip_no_server
async def test_message_has_seq():
    """Live messages should carry a seq tag from the server."""
    c1 = await _make_client("test-token-1", "agent-1")
    c2 = await _make_client("test-token-2", "agent-2")
    try:
        await c1.join("#test-seq")
        await c2.join("#test-seq")
        await asyncio.sleep(0.2)

        await c1.privmsg("#test-seq", "seq test")

        async for msg in c2.messages():
            if "seq test" in msg.content:
                assert msg.seq is not None, "Live message should have seq from @seq tag"
                assert msg.seq > 0
                break
    finally:
        await c1.close()
        await c2.close()


# ── Concurrent TASK CLAIM ──────────────────────────────────────


@skip_no_server
async def test_concurrent_task_claim():
    """3 agents race to claim the same task -- exactly 1 must succeed.

    Server format:
      Success broadcast: NOTICE #channel :TASK <id> claimed by <nick>: <title>
      Failure to caller: NOTICE <nick> :TASK CLAIM <id> failed: <reason>
    """
    c1 = await _make_client("test-token-1", "agent-1")
    c2 = await _make_client("test-token-2", "agent-2")
    c3 = await _make_client("test-token-3", "agent-3")
    try:
        channel = "#test-claim"
        await c1.join(channel)
        await c2.join(channel)
        await c3.join(channel)
        await asyncio.sleep(0.2)

        await c1.task_create(channel, "contested task")
        await asyncio.sleep(0.5)

        # Server broadcasts: NOTICE #channel :TASK <task_id> created by <nick>: <title>
        task_id = await _extract_task_id(c1)
        assert task_id, "Failed to get task ID from server"

        # All 3 agents race to claim
        results = await asyncio.gather(
            _try_claim(c1, task_id),
            _try_claim(c2, task_id),
            _try_claim(c3, task_id),
        )

        winners = [r for r in results if r]
        assert len(winners) == 1, f"Expected exactly 1 winner, got {len(winners)}: {winners}"
    finally:
        await c1.close()
        await c2.close()
        await c3.close()


async def _extract_task_id(client: AircdClient) -> str | None:
    """Read messages until we find a task creation notice with an ID.

    Server format: "TASK <task_id> created by <nick>: <title>"
    """
    async for msg in client.messages():
        # Match "TASK task_<uuid> created by ..."
        match = re.search(r"TASK\s+(task_\S+)\s+created\s+by", msg.content, re.IGNORECASE)
        if match:
            return match.group(1)
    return None


async def _try_claim(client: AircdClient, task_id: str) -> str | None:
    """Attempt to claim a task. Returns the agent nick if successful, None otherwise.

    Server responses:
      Success: NOTICE #channel :TASK <id> claimed by <nick>: <title>
      Failure: NOTICE <nick> :TASK CLAIM <id> failed: <reason>
    """
    await client.task_claim(task_id)
    async for msg in client.messages():
        if task_id not in msg.content:
            continue
        content_lower = msg.content.lower()
        if "claimed by" in content_lower:
            return client.nick
        if "fail" in content_lower:
            return None
    return None


# ── Disconnect/Reconnect + History Replay ───────────────────────


@skip_no_server
async def test_reconnect_history_replay():
    """Agent disconnects, messages are sent, agent reconnects and gets missed messages.

    Tests the bouncer behavior: server tracks last_seen_seq per principal,
    auto-replays missed messages on reconnect with @replay=1 tag.
    """
    c1 = await _make_client("test-token-1", "agent-1")
    c2 = await _make_client("test-token-2", "agent-2")
    try:
        channel = "#test-replay"
        await c1.join(channel)
        await c2.join(channel)
        await asyncio.sleep(0.3)

        # Drain any existing messages from prior test runs
        await c1.privmsg(channel, "before disconnect")
        await asyncio.sleep(0.3)

        # Agent-2 disconnects
        await c2.close()
        await asyncio.sleep(0.3)

        # Agent-1 sends messages while agent-2 is offline
        await c1.privmsg(channel, "missed msg 1")
        await c1.privmsg(channel, "missed msg 2")
        await c1.privmsg(channel, "missed msg 3")
        await asyncio.sleep(0.3)

        # Agent-2 reconnects — server auto-replays missed messages
        c2 = await _make_client("test-token-2", "agent-2")
        # No need to rejoin — durable membership preserved by server
        await asyncio.sleep(0.5)

        missed = []
        async for msg in c2.messages():
            if "missed msg" in msg.content:
                missed.append(msg.content)
                assert msg.is_replay, "Replayed message should have is_replay=True"
            if len(missed) >= 3:
                break

        assert len(missed) == 3, f"Expected 3 missed messages, got {len(missed)}: {missed}"
    finally:
        await c1.close()
        await c2.close()


# ── Task Lease Expiry ───────────────────────────────────────────


@skip_no_server
async def test_task_lease_expiry():
    """Claimed task returns to open after lease expires, allowing re-claim.

    Server uses lazy reclaim in TASK CLAIM: if lease_expires_at < now,
    the claim succeeds even if status is 'claimed'. Default lease is 300s,
    so this test needs a short lease config or waits the full period.
    """
    c1 = await _make_client("test-token-1", "agent-1")
    c2 = await _make_client("test-token-2", "agent-2")
    try:
        channel = "#test-lease"
        await c1.join(channel)
        await c2.join(channel)
        await asyncio.sleep(0.2)

        await c1.task_create(channel, "expiring task")
        await asyncio.sleep(0.5)
        task_id = await _extract_task_id(c1)
        assert task_id

        result = await _try_claim(c1, task_id)
        assert result == "agent-1", "agent-1 should claim the task"

        # Wait for lease to expire (server default 300s, test config should be shorter)
        # Skip this test if lease is too long for CI
        lease_wait = int(os.environ.get("AIRCD_LEASE_WAIT_SECONDS", "12"))
        await asyncio.sleep(lease_wait)

        result2 = await _try_claim(c2, task_id)
        assert result2 == "agent-2", "agent-2 should claim after lease expiry"
    finally:
        await c1.close()
        await c2.close()
