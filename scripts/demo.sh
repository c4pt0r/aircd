#!/usr/bin/env bash
#
# aircd protocol demo: build server, start it, and run protocol-level E2E
# examples for broadcast, message history, and collaborative task flow.
#
# Usage:
#   ./scripts/demo.sh
#
# Prerequisites:
#   - Rust toolchain (cargo)
#   - Python 3.10+ with venv support
#
# The script builds the server, starts it on a temp DB, runs the Python
# protocol demo, then cleans up. Exit code 0 = demo passed.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
AIRCD_DB="$(mktemp /tmp/aircd-demo-XXXXXX.sqlite3)"
AIRCD_PID=""

cleanup() {
    if [ -n "$AIRCD_PID" ] && kill -0 "$AIRCD_PID" 2>/dev/null; then
        kill "$AIRCD_PID" 2>/dev/null || true
        wait "$AIRCD_PID" 2>/dev/null || true
    fi
    rm -f "$AIRCD_DB"
}
trap cleanup EXIT

echo "=== aircd protocol demo ==="
echo ""

# Step 1: Build server
echo "[1/4] Building server..."
cd "$REPO_ROOT"
cargo build --release --quiet
echo "  Server built."

# Step 2: Start server
echo "[2/4] Starting server..."
AIRCD_DB="$AIRCD_DB" ./target/release/aircd &
AIRCD_PID=$!
sleep 1

if ! kill -0 "$AIRCD_PID" 2>/dev/null; then
    echo "  ERROR: Server failed to start."
    exit 1
fi
echo "  Server running (PID $AIRCD_PID)."

# Step 3: Set up Python client
echo "[3/4] Setting up Python client..."
cd "$REPO_ROOT/clients/python"
if [ ! -d .venv ]; then
    python3 -m venv .venv
fi
.venv/bin/pip install -e ".[dev]" --quiet
echo "  Python client ready."

# Step 4: Run protocol examples
echo "[4/4] Running protocol E2E examples..."
echo ""

.venv/bin/python - <<'PY'
import asyncio
import re
import sys
import traceback

sys.path.insert(0, ".")
from aircd import AircdClient

HOST, PORT = "localhost", 6667


async def make_client(token: str, nick: str) -> AircdClient:
    client = AircdClient(HOST, PORT, token=token, nick=nick, auto_reconnect=False)
    await client.connect()
    return client


async def close_clients(*clients: AircdClient | None) -> None:
    for client in clients:
        if client is not None:
            await client.close()


async def wait_for_message(client: AircdClient, predicate, *, timeout: float, description: str):
    async def _wait():
        async for msg in client.messages():
            if predicate(msg):
                return msg

    try:
        return await asyncio.wait_for(_wait(), timeout=timeout)
    except asyncio.TimeoutError as exc:
        raise AssertionError(f"{client.nick}: timeout waiting for {description}") from exc


def extract_task_id(content: str) -> str | None:
    match = re.search(r"TASK\s+(task_\S+)\s+", content, re.IGNORECASE)
    return match.group(1) if match else None


async def demo_broadcast() -> None:
    print("[example 1/3] broadcast fan-out")
    channel = "#demo-broadcast"
    sender = receiver_a = receiver_b = None
    try:
        sender = await make_client("human-token", "human")
        receiver_a = await make_client("agent-a-token", "agent-a")
        receiver_b = await make_client("agent-b-token", "agent-b")

        for client in (sender, receiver_a, receiver_b):
            await client.join(channel)
        await asyncio.sleep(0.3)

        text = "broadcast example: hello everyone"
        await sender.privmsg(channel, text)

        msg_a = await wait_for_message(
            receiver_a,
            lambda msg: msg.channel == channel and msg.sender == "human" and msg.content == text,
            timeout=5.0,
            description="broadcast to agent-a",
        )
        msg_b = await wait_for_message(
            receiver_b,
            lambda msg: msg.channel == channel and msg.sender == "human" and msg.content == text,
            timeout=5.0,
            description="broadcast to agent-b",
        )

        assert not msg_a.is_replay, "broadcast delivery to agent-a should be live"
        assert not msg_b.is_replay, "broadcast delivery to agent-b should be live"
        print("  PASS: one channel message reached multiple subscribers")
    finally:
        await close_clients(sender, receiver_a, receiver_b)


async def demo_history() -> None:
    print("[example 2/3] reconnect history replay")
    channel = "#demo-history"
    sender = reader = None
    try:
        sender = await make_client("agent-a-token", "agent-a")
        reader = await make_client("agent-b-token", "agent-b")

        for client in (sender, reader):
            await client.join(channel)
        await asyncio.sleep(0.3)

        await reader.close()
        reader = None
        await asyncio.sleep(0.3)

        history_texts = ["history replay 1", "history replay 2", "history replay 3"]
        for text in history_texts:
            await sender.privmsg(channel, text)
        await asyncio.sleep(0.3)

        reader = await make_client("agent-b-token", "agent-b")
        await asyncio.sleep(0.5)

        replayed = []
        for text in history_texts:
            msg = await wait_for_message(
                reader,
                lambda candidate, text=text: (
                    candidate.channel == channel
                    and candidate.sender == "agent-a"
                    and candidate.content == text
                    and candidate.is_replay
                ),
                timeout=5.0,
                description=f"replayed history for {text!r}",
            )
            replayed.append(msg.content)

        assert replayed == history_texts, f"unexpected replay order: {replayed}"
        print("  PASS: reconnect replay returned missed messages with replay tags")
    finally:
        await close_clients(sender, reader)


async def demo_collaboration() -> None:
    print("[example 3/3] collaborative task flow")
    channel = "#demo-collab"
    creator = worker = observer = None
    try:
        creator = await make_client("human-token", "human")
        worker = await make_client("agent-a-token", "agent-a")
        observer = await make_client("agent-c-token", "agent-c")

        for client in (creator, worker, observer):
            await client.join(channel)
        await asyncio.sleep(0.3)

        title = "investigate flaky build"
        await creator.task_create(channel, title)

        created = await wait_for_message(
            creator,
            lambda msg: msg.channel == channel and "created by human" in msg.content.lower() and title in msg.content,
            timeout=5.0,
            description="task creation broadcast",
        )
        task_id = extract_task_id(created.content)
        assert task_id is not None, f"could not parse task id from {created.content!r}"

        await worker.task_claim(task_id)

        claim_description = f"claim broadcast for {task_id}"
        claim_predicate = lambda msg: msg.channel == channel and task_id in msg.content and "claimed by agent-a" in msg.content.lower()
        await wait_for_message(creator, claim_predicate, timeout=5.0, description=claim_description)
        await wait_for_message(observer, claim_predicate, timeout=5.0, description=claim_description)

        await creator.task_list(channel)
        listed_claimed = await wait_for_message(
            creator,
            lambda msg: task_id in msg.content and "status=claimed" in msg.content and "claimed_by=agent-a" in msg.content,
            timeout=5.0,
            description="task list entry showing claimed status",
        )
        assert title in listed_claimed.content, "task list should include the original title"

        await worker.task_done(task_id)

        done_description = f"completion broadcast for {task_id}"
        done_predicate = lambda msg: msg.channel == channel and task_id in msg.content and "completed by agent-a" in msg.content.lower()
        await wait_for_message(creator, done_predicate, timeout=5.0, description=done_description)
        await wait_for_message(observer, done_predicate, timeout=5.0, description=done_description)

        await creator.task_list(channel)
        empty_list = await wait_for_message(
            creator,
            lambda msg: msg.content == f"No open tasks in {channel}",
            timeout=5.0,
            description="empty task list after completion",
        )
        assert empty_list.sender == "aircd", "task list response should come from the server"
        print("  PASS: create → claim → done was visible to collaborators and task list")
    finally:
        await close_clients(creator, worker, observer)


async def main() -> bool:
    try:
        await demo_broadcast()
        print("")
        await demo_history()
        print("")
        await demo_collaboration()
        print("")
        print("  PASS: all protocol E2E examples succeeded")
        return True
    except AssertionError as exc:
        print(f"  FAIL: {exc}")
        return False
    except Exception:
        traceback.print_exc()
        return False


ok = asyncio.run(main())
sys.exit(0 if ok else 1)
PY

echo "=== Demo complete ==="
