#!/usr/bin/env bash
#
# aircd demo: build server, start it, run 3-agent concurrent claim race.
#
# Usage:
#   ./scripts/demo.sh
#
# Prerequisites:
#   - Rust toolchain (cargo)
#   - Python 3.10+ with venv support
#
# The script builds the server, starts it on a temp DB, runs the Python
# concurrent claim demo, then cleans up. Exit code 0 = demo passed.

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

echo "=== aircd demo ==="
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

# Step 4: Run concurrent claim demo
echo "[4/4] Running 3-agent concurrent claim race..."
echo ""

.venv/bin/python -c "
import asyncio
import re
import sys
sys.path.insert(0, '.')
from aircd import AircdClient

HOST, PORT = 'localhost', 6667
CHANNEL = '#demo-race'

AGENTS = [
    ('agent-a-token', 'agent-a'),
    ('agent-b-token', 'agent-b'),
    ('agent-c-token', 'agent-c'),
]

async def race():
    # Create task
    creator = AircdClient(HOST, PORT, token='agent-a-token', nick='agent-a', auto_reconnect=False)
    await creator.connect()
    await creator.join(CHANNEL)
    await asyncio.sleep(0.2)

    await creator.task_create(CHANNEL, 'demo contested task')
    await asyncio.sleep(0.5)

    task_id = None
    async for msg in creator.messages():
        m = re.search(r'TASK\s+(task_\S+)\s+created\s+by', msg.content)
        if m:
            task_id = m.group(1)
            break
    await creator.close()

    if not task_id:
        print('  FAIL: Could not create task')
        return False

    print(f'  Task created: {task_id}')
    print(f'  3 agents racing to claim...')

    # Race
    results = {}

    async def try_claim(token, nick):
        c = AircdClient(HOST, PORT, token=token, nick=nick, auto_reconnect=False)
        await c.connect()
        await c.join(CHANNEL)
        await asyncio.sleep(0.1)
        await c.task_claim(task_id)
        async for msg in c.messages():
            if task_id not in msg.content:
                continue
            if 'fail' in msg.content.lower():
                results[nick] = False
                break
            m2 = re.search(r'claimed\s+by\s+(\S+)', msg.content.lower())
            if m2:
                claimer = m2.group(1).rstrip(':')
                results[nick] = (claimer == nick)
                break
        await c.close()

    await asyncio.gather(*(try_claim(t, n) for t, n in AGENTS))

    winners = [n for n, won in results.items() if won]
    losers = [n for n, won in results.items() if not won]

    for nick, won in sorted(results.items()):
        status = 'WON' if won else 'lost'
        print(f'    {nick}: {status}')

    print()
    if len(winners) == 1:
        print(f'  PASS: Exactly 1 winner ({winners[0]}) — atomic claim works!')
        return True
    else:
        print(f'  FAIL: Expected 1 winner, got {len(winners)}: {winners}')
        return False

ok = asyncio.run(race())
sys.exit(0 if ok else 1)
"

echo ""
echo "=== Demo complete ==="
