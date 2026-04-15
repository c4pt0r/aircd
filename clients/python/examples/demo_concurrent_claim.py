"""Demo: 3 agents race to claim the same task — only 1 should win.

Usage:
    python demo_concurrent_claim.py

Requires:
  - Running aircd server on localhost:6667
  - Pre-configured tokens: test-token-1, test-token-2, test-token-3
"""

from __future__ import annotations

import asyncio
import logging
import re
import sys

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent.parent))

from aircd import AircdClient

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(message)s")
logger = logging.getLogger("race")

HOST = "localhost"
PORT = 6667
CHANNEL = "#race-test"

AGENTS = [
    ("test-token-1", "agent-1"),
    ("test-token-2", "agent-2"),
    ("test-token-3", "agent-3"),
]


async def agent_racer(token: str, nick: str, task_id: str, results: dict[str, bool]) -> None:
    """Connect an agent and try to claim the given task."""
    client = AircdClient(HOST, PORT, token=token, nick=nick, auto_reconnect=False)
    await client.connect()
    await client.join(CHANNEL)
    await asyncio.sleep(0.1)

    logger.info("[%s] Attempting TASK CLAIM %s", nick, task_id)
    await client.task_claim(task_id)

    # Read response — server sends:
    #   Success: NOTICE #channel :TASK <id> claimed by <nick>: <title>
    #   Failure: NOTICE <nick> :TASK CLAIM <id> failed: <reason>
    async for msg in client.messages():
        if task_id in msg.content:
            if "claimed by" in msg.content.lower():
                results[nick] = True
                logger.info("[%s] WON the claim!", nick)
            elif "fail" in msg.content.lower():
                results[nick] = False
                logger.info("[%s] Lost the claim: %s", nick, msg.content)
            break

    await client.close()


async def main() -> None:
    # Step 1: Create a task using agent-1
    creator = AircdClient(HOST, PORT, token="test-token-1", nick="agent-1", auto_reconnect=False)
    await creator.connect()
    await creator.join(CHANNEL)
    await asyncio.sleep(0.2)

    await creator.task_create(CHANNEL, "contested task for race test")
    await asyncio.sleep(0.5)

    # Extract task ID
    task_id = None
    async for msg in creator.messages():
        match = re.search(r"TASK\s+(task_\S+)\s+created\s+by", msg.content)
        if match:
            task_id = match.group(1)
            break
    await creator.close()

    if not task_id:
        logger.error("Failed to create task / extract ID")
        return

    logger.info("Created task: %s", task_id)
    logger.info("Starting race with 3 agents...")

    # Step 2: 3 agents race to claim
    results: dict[str, bool] = {}
    await asyncio.gather(
        *(agent_racer(token, nick, task_id, results) for token, nick in AGENTS)
    )

    # Step 3: Verify exactly 1 winner
    winners = [nick for nick, won in results.items() if won]
    logger.info("Results: %s", results)
    logger.info("Winners: %s", winners)

    if len(winners) == 1:
        logger.info("PASS: Exactly 1 agent won the race (atomic claim works)")
    else:
        logger.error("FAIL: Expected 1 winner, got %d: %s", len(winners), winners)


if __name__ == "__main__":
    asyncio.run(main())
