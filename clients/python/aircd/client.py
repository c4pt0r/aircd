"""Async IRC client for aircd server.

Handles connection, authentication, channel operations, message history
replay (bouncer), and task primitives. Designed for agent use cases where
reliability and automatic reconnection matter more than full IRC compliance.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Callable, Optional

logger = logging.getLogger("aircd")


@dataclass
class Message:
    """A message received from the server."""

    seq: Optional[int]  # sequence number (if from history/durable log)
    channel: str
    sender: str
    content: str
    raw: str  # original IRC line
    is_replay: bool = False  # True if this message came from CHATHISTORY replay


@dataclass
class Task:
    """A task from the TASK system."""

    id: str
    channel: str
    title: str
    status: str  # open, claimed, done
    claimed_by: Optional[str] = None


@dataclass
class _ConnectionState:
    reader: Optional[asyncio.StreamReader] = None
    writer: Optional[asyncio.StreamWriter] = None
    registered: bool = False
    nick: Optional[str] = None
    channels: set[str] = field(default_factory=set)
    last_seen_seq: dict[str, int] = field(default_factory=dict)  # channel -> seq


class AircdClient:
    """Async client for aircd server.

    Usage::

        client = AircdClient("localhost", 6667, token="agent-token-1", nick="agent-1")
        await client.connect()
        await client.join("#work")
        await client.privmsg("#work", "hello from agent")

        # Listen for messages
        async for msg in client.messages():
            print(f"{msg.sender}: {msg.content}")

    For agent use cases with auto-reconnect::

        client = AircdClient(..., auto_reconnect=True)
        await client.connect()
        # If connection drops, client reconnects and replays missed messages
    """

    def __init__(
        self,
        host: str,
        port: int,
        token: str,
        nick: str,
        *,
        auto_reconnect: bool = True,
        reconnect_delay: float = 2.0,
        max_reconnect_delay: float = 60.0,
    ):
        self.host = host
        self.port = port
        self.token = token
        self.nick = nick
        self.auto_reconnect = auto_reconnect
        self.reconnect_delay = reconnect_delay
        self.max_reconnect_delay = max_reconnect_delay

        self._state = _ConnectionState()
        self._message_queue: asyncio.Queue[Message] = asyncio.Queue()
        self._raw_handlers: list[Callable[[str], None]] = []
        self._read_task: Optional[asyncio.Task] = None
        self._closed = False

    # ── Connection ──────────────────────────────────────────────

    async def connect(self) -> None:
        """Connect to the server, authenticate, and register."""
        self._closed = False
        await self._do_connect()

    async def _do_connect(self) -> None:
        reader, writer = await asyncio.open_connection(self.host, self.port)
        self._state.reader = reader
        self._state.writer = writer
        self._state.registered = False

        # Authenticate: PASS -> NICK -> USER
        await self._send(f"PASS {self.token}")
        await self._send(f"NICK {self.nick}")
        await self._send(f"USER {self.nick} 0 * :aircd agent")

        # Wait for registration confirmation (001 RPL_WELCOME or ERR)
        while not self._state.registered:
            line = await self._readline()
            if line is None:
                raise ConnectionError("Connection closed during registration")
            _prefix, command, params, _tags = _parse_irc_line(line)
            if command == "001":
                self._state.registered = True
                # Server may override our nick
                if params:
                    self._state.nick = params[0]
            elif command == "PING":
                await self._send(f"PONG :{params[-1] if params else ''}")
            elif command in ("432", "433", "461", "462", "464"):
                raise ConnectionError(f"Registration failed: {line}")

        # Rejoin channels and request history replay (bouncer behavior)
        for channel in list(self._state.channels):
            await self._send(f"JOIN {channel}")
            last_seq = self._state.last_seen_seq.get(channel, 0)
            if last_seq > 0:
                await self._send(f"CHATHISTORY AFTER {channel} {last_seq} 1000")

        # Start background reader
        self._read_task = asyncio.create_task(self._reader_loop())

    async def close(self) -> None:
        """Disconnect from the server."""
        self._closed = True
        if self._read_task:
            self._read_task.cancel()
            try:
                await self._read_task
            except asyncio.CancelledError:
                pass
        if self._state.writer:
            try:
                await self._send("QUIT :bye")
                self._state.writer.close()
                await self._state.writer.wait_closed()
            except Exception:
                pass
        self._state.reader = None
        self._state.writer = None

    # ── Channel operations ──────────────────────────────────────

    async def join(self, channel: str) -> None:
        """Join a channel."""
        self._state.channels.add(channel)
        await self._send(f"JOIN {channel}")

    async def part(self, channel: str) -> None:
        """Leave a channel."""
        self._state.channels.discard(channel)
        self._state.last_seen_seq.pop(channel, None)
        await self._send(f"PART {channel}")

    # ── Messaging ───────────────────────────────────────────────

    async def privmsg(self, target: str, text: str) -> None:
        """Send a message to a channel or user."""
        await self._send(f"PRIVMSG {target} :{text}")

    async def history(self, channel: str, after_seq: int = 0, limit: int = 100) -> None:
        """Request message history for a channel. Results arrive via messages()."""
        await self._send(f"CHATHISTORY AFTER {channel} {after_seq} {limit}")

    async def messages(self):
        """Async generator yielding Message objects as they arrive.

        This is the primary way to consume messages::

            async for msg in client.messages():
                print(f"[{msg.channel}] {msg.sender}: {msg.content}")
        """
        while not self._closed:
            try:
                msg = await asyncio.wait_for(self._message_queue.get(), timeout=1.0)
                yield msg
            except asyncio.TimeoutError:
                continue

    # ── Task primitives ─────────────────────────────────────────

    async def task_create(self, channel: str, title: str) -> None:
        """Create a new task in a channel."""
        await self._send(f"TASK CREATE {channel} :{title}")

    async def task_claim(self, task_id: str) -> None:
        """Atomically claim a task. Server ensures only one agent succeeds."""
        await self._send(f"TASK CLAIM {task_id}")

    async def task_done(self, task_id: str) -> None:
        """Mark a claimed task as done."""
        await self._send(f"TASK DONE {task_id}")

    async def task_release(self, task_id: str) -> None:
        """Release a claimed task back to open."""
        await self._send(f"TASK RELEASE {task_id}")

    async def task_list(self, channel: str) -> None:
        """List tasks in a channel. Results arrive as server notices."""
        await self._send(f"TASK LIST {channel}")

    # ── Internal ────────────────────────────────────────────────

    async def _send(self, line: str) -> None:
        if self._state.writer is None:
            raise ConnectionError("Not connected")
        self._state.writer.write((line + "\r\n").encode("utf-8"))
        await self._state.writer.drain()
        logger.debug(">> %s", line)

    async def _readline(self) -> Optional[str]:
        if self._state.reader is None:
            return None
        try:
            data = await self._state.reader.readline()
            if not data:
                return None
            line = data.decode("utf-8", errors="replace").rstrip("\r\n")
            logger.debug("<< %s", line)
            return line
        except (ConnectionError, asyncio.IncompleteReadError):
            return None

    async def _reader_loop(self) -> None:
        """Background loop: read lines, dispatch PING/PONG, queue messages."""
        delay = self.reconnect_delay
        while not self._closed:
            try:
                line = await self._readline()
                if line is None:
                    raise ConnectionError("Connection lost")

                prefix, command, params, tags = _parse_irc_line(line)

                if command == "PING":
                    await self._send(f"PONG :{params[-1] if params else ''}")
                elif command == "PRIVMSG" and len(params) >= 2:
                    sender_nick = _extract_nick(prefix)
                    # Extract seq from message tags (e.g. @seq=42)
                    seq = None
                    if "seq" in tags:
                        try:
                            seq = int(tags["seq"])
                        except ValueError:
                            pass
                    is_replay = tags.get("replay") == "1" or "batch" in tags
                    msg = Message(
                        seq=seq,
                        channel=params[0],
                        sender=sender_nick,
                        content=params[1],
                        raw=line,
                        is_replay=is_replay,
                    )
                    # Track last seen seq for bouncer replay
                    if msg.seq is not None and msg.channel.startswith("#"):
                        self._state.last_seen_seq[msg.channel] = max(
                            self._state.last_seen_seq.get(msg.channel, 0),
                            msg.seq,
                        )
                    self._message_queue.put_nowait(msg)

                # Notify raw handlers
                for handler in self._raw_handlers:
                    handler(line)

                # Reset reconnect delay on successful read
                delay = self.reconnect_delay

            except (ConnectionError, OSError) as e:
                if self._closed:
                    return
                if not self.auto_reconnect:
                    logger.error("Connection lost: %s", e)
                    return

                logger.warning("Connection lost, reconnecting in %.1fs: %s", delay, e)
                await asyncio.sleep(delay)
                delay = min(delay * 2, self.max_reconnect_delay)
                try:
                    await self._do_connect()
                    return  # _do_connect starts a new reader loop
                except Exception as re:
                    logger.error("Reconnect failed: %s", re)
                    continue

            except asyncio.CancelledError:
                return


# ── IRC line parser ─────────────────────────────────────────────


def _parse_irc_line(line: str) -> tuple[str, str, list[str], dict[str, str]]:
    """Parse an IRC protocol line into (prefix, command, params, tags).

    Returns:
        (prefix, command, [params...], {tag_key: tag_value})
        prefix is empty string if not present.
        tags is empty dict if no message tags.
    """
    prefix = ""
    trailing = ""
    tags: dict[str, str] = {}

    if line.startswith("@"):
        tag_end = line.index(" ")
        tag_str = line[1:tag_end]
        for part in tag_str.split(";"):
            if "=" in part:
                k, v = part.split("=", 1)
                tags[k] = v
            else:
                tags[part] = ""
        line = line[tag_end + 1 :]

    if line.startswith(":"):
        space = line.index(" ")
        prefix = line[1:space]
        line = line[space + 1 :]

    if " :" in line:
        idx = line.index(" :")
        trailing = line[idx + 2 :]
        line = line[:idx]

    parts = line.split()
    command = parts[0].upper() if parts else ""
    params = parts[1:] if len(parts) > 1 else []
    if trailing:
        params.append(trailing)

    return prefix, command, params, tags


def _extract_nick(prefix: str) -> str:
    """Extract nick from IRC prefix like 'nick!user@host'."""
    if "!" in prefix:
        return prefix.split("!")[0]
    return prefix
