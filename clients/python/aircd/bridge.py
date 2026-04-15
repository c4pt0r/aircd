"""MCP chat bridge for aircd.

Stdio-based MCP server that provides messaging tools to Claude Code.
Communicates with the aircd daemon's local HTTP API to access the
pending inbox and send IRC messages.

This is a delivery adapter, not a message store. Authoritative history
lives in the aircd IRC server.
"""

from __future__ import annotations

import json
import os
import urllib.parse
import urllib.request
import urllib.error
from mcp.server.fastmcp import FastMCP

DAEMON_URL = os.environ.get("AIRCD_DAEMON_URL", "http://127.0.0.1:7667")

mcp = FastMCP("aircd-chat")


def _daemon_get(path: str) -> dict:
    """GET request to daemon local HTTP API."""
    url = f"{DAEMON_URL}{path}"
    req = urllib.request.Request(url)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read())
    except urllib.error.URLError as e:
        return {"error": str(e)}


def _daemon_post(path: str, body: dict) -> dict:
    """POST request to daemon local HTTP API."""
    url = f"{DAEMON_URL}{path}"
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read())
    except urllib.error.URLError as e:
        return {"error": str(e)}


def _format_message(msg: dict) -> str:
    """Format a message dict into slock-style envelope."""
    target = msg.get("channel", "")
    msg_id = msg.get("msg_id", "")
    time_val = msg.get("time", "")
    sender = msg.get("sender", "")
    content = msg.get("content", "")
    msg_type = msg.get("type", "")

    parts = [f"target={target}"]
    if msg_id:
        parts.append(f"msg={msg_id}")
    if time_val:
        parts.append(f"time={time_val}")
    if msg_type:
        parts.append(f"type={msg_type}")

    envelope = " ".join(parts)
    return f"[{envelope}] @{sender}: {content}"


@mcp.tool()
def check_messages() -> str:
    """Check for new messages without waiting.

    Returns immediately with any pending messages from IRC channels,
    or 'No new messages' if none. Use this freely during work — at
    natural breakpoints, after notifications, or whenever you want
    to see if anything new came in.
    """
    result = _daemon_get("/messages/pending")
    if "error" in result:
        return f"Error checking messages: {result['error']}"

    messages = result.get("messages", [])
    if not messages:
        return "No new messages."

    lines = [_format_message(m) for m in messages]
    return "\n".join(lines)


@mcp.tool()
def send_message(target: str, content: str) -> str:
    """Send a message to a channel or user.

    Args:
        target: Where to send. Use '#channel' for channels, 'nick' for DMs.
        content: The message content.
    """
    result = _daemon_post("/messages/send", {
        "target": target,
        "content": content,
    })
    if "error" in result:
        return f"Error sending message: {result['error']}"
    return result.get("status", "sent")


@mcp.tool()
def read_history(channel: str, limit: int = 50, after_seq: int = 0) -> str:
    """Read message history for a channel from the aircd server.

    History is fetched from the server via CHATHISTORY, not from local state.

    Args:
        channel: Channel name (e.g. '#general').
        limit: Maximum number of messages to return (default 50).
        after_seq: Only return messages after this sequence number (default 0 = all).
    """
    params = urllib.parse.urlencode({
        "channel": channel, "after_seq": after_seq, "limit": limit,
    })
    result = _daemon_get(f"/history?{params}")
    if "error" in result:
        return f"Error reading history: {result['error']}"

    messages = result.get("messages", [])
    if not messages:
        return f"No messages in {channel} after seq {after_seq}."

    lines = [_format_message(m) for m in messages]
    return "\n".join(lines)


@mcp.tool()
def list_server() -> str:
    """List all channels and their members on the aircd server."""
    result = _daemon_get("/server/info")
    if "error" in result:
        return f"Error listing server: {result['error']}"

    channels = result.get("channels", [])
    agents = result.get("agents", [])

    parts = []
    if channels:
        parts.append("## Channels")
        for ch in channels:
            name = ch.get("name", "")
            members = ", ".join(ch.get("members", []))
            parts.append(f"- {name}: {members}")

    if agents:
        parts.append("\n## Agents")
        for a in agents:
            parts.append(f"- {a}")

    return "\n".join(parts) if parts else "No channels or agents found."


@mcp.tool()
def list_tasks(channel: str) -> str:
    """List tasks in a channel.

    Args:
        channel: Channel name (e.g. '#work').
    """
    params = urllib.parse.urlencode({"channel": channel})
    result = _daemon_get(f"/tasks?{params}")
    if "error" in result:
        return f"Error listing tasks: {result['error']}"

    tasks = result.get("tasks", [])
    if not tasks:
        return f"No open tasks in {channel}."

    lines = []
    for t in tasks:
        tid = t.get("id", "")
        title = t.get("title", "")
        status = t.get("status", "")
        claimed = t.get("claimed_by", "-")
        lines.append(f"- {tid} [{status}] {title} (claimed: {claimed})")
    return "\n".join(lines)


@mcp.tool()
def claim_task(task_id: str) -> str:
    """Atomically claim a task. Server ensures only one agent succeeds.

    Args:
        task_id: The task ID to claim (e.g. 'task_abc123').
    """
    result = _daemon_post("/tasks/claim", {"task_id": task_id})
    if "error" in result:
        return f"Error claiming task: {result['error']}"
    return result.get("status", "claimed")


@mcp.tool()
def complete_task(task_id: str) -> str:
    """Mark a claimed task as done.

    Args:
        task_id: The task ID to complete.
    """
    result = _daemon_post("/tasks/done", {"task_id": task_id})
    if "error" in result:
        return f"Error completing task: {result['error']}"
    return result.get("status", "done")


def main():
    """Run the MCP bridge as a stdio server."""
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
