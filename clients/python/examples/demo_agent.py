"""Demo: agent connects to aircd, joins a channel, and processes tasks.

Usage:
    python demo_agent.py --token test-token-1 --nick agent-1 --channel "#work"

Requires a running aircd server on localhost:6667.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import re
import sys

# Add parent directory to path for development
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent.parent))

from aircd import AircdClient

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(message)s")
logger = logging.getLogger("demo")


async def run_agent(host: str, port: int, token: str, nick: str, channel: str) -> None:
    client = AircdClient(host, port, token=token, nick=nick, auto_reconnect=True)
    await client.connect()
    logger.info("Connected as %s", nick)

    await client.join(channel)
    logger.info("Joined %s", channel)

    await client.privmsg(channel, f"{nick} online, ready for tasks")

    async for msg in client.messages():
        logger.info("[%s] %s: %s", msg.channel, msg.sender, msg.content)

        # Auto-claim tasks that appear
        match = re.search(r"TASK[_ ]CREATED?\s+(\S+)", msg.content, re.IGNORECASE)
        if match:
            task_id = match.group(1)
            logger.info("Attempting to claim task %s", task_id)
            await client.task_claim(task_id)

        # Acknowledge successful claims
        if "claimed" in msg.content.lower() and nick in msg.content:
            logger.info("Successfully claimed a task! Working on it...")
            await asyncio.sleep(2)  # simulate work
            # Extract task ID and mark done
            done_match = re.search(r"(\S+)\s+claimed", msg.content, re.IGNORECASE)
            if done_match:
                await client.task_done(done_match.group(1))
                logger.info("Task completed")


def main() -> None:
    parser = argparse.ArgumentParser(description="aircd demo agent")
    parser.add_argument("--host", default="localhost")
    parser.add_argument("--port", type=int, default=6667)
    parser.add_argument("--token", required=True)
    parser.add_argument("--nick", required=True)
    parser.add_argument("--channel", default="#work")
    args = parser.parse_args()

    asyncio.run(run_agent(args.host, args.port, args.token, args.nick, args.channel))


if __name__ == "__main__":
    main()
