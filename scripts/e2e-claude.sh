#!/usr/bin/env bash
#
# aircd E2E test: Claude agent receives and replies to messages.
#
# This starts the aircd server, connects a Claude agent via aircd-daemon,
# then sends a message as a "human" principal. The test verifies that the
# Claude agent receives the message and sends a reply visible to everyone.
#
# Usage:
#   ./scripts/e2e-claude.sh
#
# Prerequisites:
#   - Rust toolchain (cargo)
#   - Python 3.10+ with venv
#   - `claude` CLI installed and accessible in PATH
#
# Environment:
#   CLAUDE_MODEL  - model to use (default: sonnet)

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
AIRCD_DB="$(mktemp /tmp/aircd-e2e-XXXXXX.sqlite3)"
AIRCD_PID=""
DAEMON_PID=""
CHANNEL="#e2e-test"

cleanup() {
    echo ""
    echo "=== Cleaning up ==="
    if [ -n "$DAEMON_PID" ] && kill -0 "$DAEMON_PID" 2>/dev/null; then
        kill "$DAEMON_PID" 2>/dev/null || true
        wait "$DAEMON_PID" 2>/dev/null || true
    fi
    if [ -n "$AIRCD_PID" ] && kill -0 "$AIRCD_PID" 2>/dev/null; then
        kill "$AIRCD_PID" 2>/dev/null || true
        wait "$AIRCD_PID" 2>/dev/null || true
    fi
    rm -f "$AIRCD_DB"
}
trap cleanup EXIT

MODEL="${CLAUDE_MODEL:-sonnet}"

echo "=== aircd E2E: Claude Agent Test ==="
echo ""

# Step 1: Build server
echo "[1/5] Building server..."
cd "$REPO_ROOT"
cargo build --release --quiet
echo "  Server built."

# Step 2: Start server
echo "[2/5] Starting server..."
AIRCD_DB="$AIRCD_DB" ./target/release/aircd &
AIRCD_PID=$!
sleep 1

if ! kill -0 "$AIRCD_PID" 2>/dev/null; then
    echo "  ERROR: Server failed to start."
    exit 1
fi
echo "  Server running (PID $AIRCD_PID)."

# Step 3: Set up Python client
echo "[3/5] Setting up Python client..."
cd "$REPO_ROOT/clients/python"
if [ ! -d .venv ]; then
    python3 -m venv .venv
fi
.venv/bin/pip install -e ".[daemon,dev]" --quiet
echo "  Python client ready."

# Step 4: Start aircd-daemon with Claude
echo "[4/5] Starting aircd-daemon (Claude agent)..."
echo "  Model: $MODEL"
echo "  Channel: $CHANNEL"

.venv/bin/python -m aircd.daemon \
    --host localhost --port 6667 \
    --token agent-a-token --nick agent-a \
    --channels "$CHANNEL" \
    --model "$MODEL" \
    --permissions-mode skip \
    --verbose \
    > /tmp/aircd-daemon-e2e.log 2>&1 &
DAEMON_PID=$!
sleep 3

if ! kill -0 "$DAEMON_PID" 2>/dev/null; then
    echo "  ERROR: Daemon failed to start. Log:"
    cat /tmp/aircd-daemon-e2e.log
    exit 1
fi
echo "  Daemon running (PID $DAEMON_PID)."

# Step 5: Human sends message, wait for agent reply
echo "[5/5] Running E2E test..."
echo ""

.venv/bin/python -c "
import asyncio
import sys
import time
sys.path.insert(0, '.')
from aircd import AircdClient

CHANNEL = '$CHANNEL'
TIMEOUT = 30  # seconds to wait for agent reply

async def e2e_test():
    # Connect as human
    human = AircdClient('localhost', 6667, token='human-token', nick='human', auto_reconnect=False)
    await human.connect()
    await human.join(CHANNEL)
    await asyncio.sleep(1)

    # Send test message
    test_msg = 'Hello agent! Please reply with a short greeting.'
    print(f'  Human sends: {test_msg}')
    await human.privmsg(CHANNEL, test_msg)

    # Wait for agent reply with a real wall-clock timeout
    print(f'  Waiting for agent reply (timeout {TIMEOUT}s)...')
    start = time.time()
    agent_reply = None

    async def wait_for_reply():
        nonlocal agent_reply
        async for msg in human.messages():
            if msg.sender == 'agent-a' and msg.channel == CHANNEL:
                elapsed = time.time() - start
                agent_reply = msg
                print(f'  Agent replied ({elapsed:.1f}s): {msg.content[:200]}')
                return

    try:
        await asyncio.wait_for(wait_for_reply(), timeout=TIMEOUT)
    except asyncio.TimeoutError:
        pass

    await human.close()

    if agent_reply:
        print()
        print('  PASS: Claude agent received message and replied!')
        return True
    else:
        print()
        print('  FAIL: No reply from agent within timeout.')
        print('  Daemon log (last 20 lines):')
        try:
            with open('/tmp/aircd-daemon-e2e.log') as f:
                lines = f.readlines()
                for line in lines[-20:]:
                    print(f'    {line.rstrip()}')
        except Exception:
            pass
        return False

ok = asyncio.run(e2e_test())
sys.exit(0 if ok else 1)
"

echo ""
echo "=== E2E test complete ==="
