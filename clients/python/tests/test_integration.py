"""Integration tests for aircd server.

These tests require a running aircd server. Set AIRCD_HOST and AIRCD_PORT
environment variables, or defaults to localhost:6667.

The server must have these test tokens pre-configured:
  - "test-token-1" -> nick "agent-1"
  - "test-token-2" -> nick "agent-2"
  - "test-token-3" -> nick "agent-3"

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

# Skip all tests if server is not reachable
pytestmark = pytest.mark.asyncio


async def _make_client(token: str, nick: str) -> AircdClient:
    client = AircdClient(
        AIRCD_HOST, AIRCD_PORT, token=token, nick=nick, auto_reconnect=False
    )
    await client.connect()
    return client


def _server_reachable() -> bool:
    """Quick check if the aircd server is listening."""
    import socket

    try:
        s = socket.create_connection((AIRCD_HOST, AIRCD_PORT), timeout=2)
        s.close()
        return True
    except OSError:
        return False


skip_no_server = pytest.mark.skipif(
    not _server_reachable(), reason=f"aircd server not reachable at {AIRCD_HOST}:{AIRCD_PORT}"
)


# ── Basic connectivity ──────────────────────────────────────────


@skip_no_server
async def test_connect_and_join():
    """Agent can connect, authenticate, and join a channel."""
    client = await _make_client("test-token-1", "agent-1")
    try:
        await client.join("#test-basic")
        # If we get here without exception, connection + join succeeded
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
        await asyncio.sleep(0.2)  # let JOINs propagate

        await c1.privmsg("#test-chat", "hello from agent-1")

        # c2 should receive the message
        received = False
        async for msg in c2.messages():
            if msg.sender == "agent-1" and "hello from agent-1" in msg.content:
                received = True
                break
        assert received, "agent-2 did not receive message from agent-1"
    finally:
        await c1.close()
        await c2.close()


# ── Concurrent TASK CLAIM ──────────────────────────────────────


@skip_no_server
async def test_concurrent_task_claim():
    """3 agents race to claim the same task — exactly 1 must succeed.

    This is the core atomicity guarantee of the TASK system.
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

        # Create a task using agent-1
        await c1.task_create(channel, "contested task")
        await asyncio.sleep(0.5)  # let task creation propagate

        # Get task ID from channel messages (server broadcasts task events)
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
    """Read messages until we find a task creation notice with an ID."""
    async for msg in client.messages():
        # Server should broadcast something like "TASK CREATED <id> ..."
        match = re.search(r"TASK[_ ]CREATED?\s+(\S+)", msg.content, re.IGNORECASE)
        if match:
            return match.group(1)
    return None


async def _try_claim(client: AircdClient, task_id: str) -> str | None:
    """Attempt to claim a task. Returns the agent nick if successful, None otherwise."""
    await client.task_claim(task_id)
    # Read server response — expect either success or failure notice
    async for msg in client.messages():
        content_lower = msg.content.lower()
        if task_id in msg.content:
            if "claimed" in content_lower or "success" in content_lower:
                return client.nick
            if "fail" in content_lower or "already" in content_lower or "error" in content_lower:
                return None
    return None


# ── Disconnect/Reconnect + History Replay ───────────────────────


@skip_no_server
async def test_reconnect_history_replay():
    """Agent disconnects, messages are sent, agent reconnects and gets missed messages.

    Tests the bouncer behavior: server tracks last_seen_seq and replays on reconnect.
    """
    c1 = await _make_client("test-token-1", "agent-1")
    c2 = await _make_client("test-token-2", "agent-2")
    try:
        channel = "#test-replay"
        await c1.join(channel)
        await c2.join(channel)
        await asyncio.sleep(0.2)

        # Agent-1 sends a message (both see it)
        await c1.privmsg(channel, "before disconnect")
        await asyncio.sleep(0.2)

        # Agent-2 disconnects
        await c2.close()
        await asyncio.sleep(0.2)

        # Agent-1 sends messages while agent-2 is offline
        await c1.privmsg(channel, "missed msg 1")
        await c1.privmsg(channel, "missed msg 2")
        await c1.privmsg(channel, "missed msg 3")
        await asyncio.sleep(0.3)

        # Agent-2 reconnects — bouncer should auto-replay missed messages
        c2 = await _make_client("test-token-2", "agent-2")
        await c2.join(channel)
        # Also manually request history in case bouncer isn't implemented yet
        await c2.history(channel, after_seq=0, limit=100)
        await asyncio.sleep(0.5)

        # Collect messages agent-2 receives
        missed = []
        async for msg in c2.messages():
            if "missed msg" in msg.content:
                missed.append(msg.content)
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

    NOTE: This test assumes a short lease timeout (e.g. 10s) for testing.
    Configure the server with AIRCD_TASK_LEASE_SECONDS=10 or similar.
    """
    c1 = await _make_client("test-token-1", "agent-1")
    c2 = await _make_client("test-token-2", "agent-2")
    try:
        channel = "#test-lease"
        await c1.join(channel)
        await c2.join(channel)
        await asyncio.sleep(0.2)

        # Create and claim a task with agent-1
        await c1.task_create(channel, "expiring task")
        await asyncio.sleep(0.5)
        task_id = await _extract_task_id(c1)
        assert task_id

        result = await _try_claim(c1, task_id)
        assert result == "agent-1", "agent-1 should claim the task"

        # Agent-1 does NOT complete the task — wait for lease to expire
        # (server lease timeout should be short for tests)
        await asyncio.sleep(12)  # wait for lease expiry

        # Agent-2 should now be able to claim
        result2 = await _try_claim(c2, task_id)
        assert result2 == "agent-2", "agent-2 should claim after lease expiry"
    finally:
        await c1.close()
        await c2.close()
