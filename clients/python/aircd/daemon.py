"""aircd local runtime wrapper (daemon).

Connects to the aircd IRC server as an agent principal, manages a local
Claude Code CLI process, and bridges messages between IRC and Claude.

This is a delivery adapter — authoritative message history and task state
live in the aircd IRC server. The daemon only maintains a temporary
delivery buffer for busy-mode notification flow.

Usage:
    python -m aircd.daemon --token agent-a-token --nick agent-a \\
        --channels '#work,#general' --host localhost --port 6667
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import urllib.parse
from argparse import ArgumentParser
from collections import deque
from dataclasses import dataclass, field
from http.server import HTTPServer, BaseHTTPRequestHandler
from threading import Thread, Lock
from typing import Optional

from aircd.client import AircdClient, Message

logger = logging.getLogger("aircd.daemon")

# How long to debounce before sending a busy notification (seconds)
BUSY_NOTIFICATION_DEBOUNCE = 3.0

# Maximum pending messages before we force-drain
MAX_PENDING = 200

# Timeout for synchronous IRC request/response (seconds)
IRC_REQUEST_TIMEOUT = 5.0


@dataclass
class AgentState:
    """Tracks the state of the managed Claude process."""

    process: Optional[subprocess.Popen] = None
    session_id: Optional[str] = None
    is_busy: bool = False
    pending_inbox: deque = field(default_factory=deque)
    last_notification_time: float = 0.0
    seen_msg_ids: set = field(default_factory=set)


@dataclass
class SyncRequest:
    """A synchronous request waiting for IRC responses."""

    event: asyncio.Event = field(default_factory=asyncio.Event)
    responses: list = field(default_factory=list)
    channel: str = ""
    done: bool = False


def find_claude_cli() -> str:
    """Find the local claude CLI binary."""
    path = shutil.which("claude")
    if path:
        return path
    raise FileNotFoundError(
        "claude CLI not found in PATH. Install Claude Code first."
    )


def encode_stdin_message(text: str, session_id: Optional[str] = None) -> str:
    """Encode a user message in Claude's stream-json stdin format."""
    msg = {
        "type": "user",
        "message": {
            "role": "user",
            "content": [{"type": "text", "text": text}],
        },
    }
    if session_id:
        msg["session_id"] = session_id
    return json.dumps(msg)


def format_envelope(msg: Message) -> str:
    """Format an IRC message into slock-style envelope string."""
    parts = [f"target={msg.channel}"]
    if msg.raw:
        m = re.search(r"msg-id=([^\s;]+)", msg.raw)
        if m:
            parts.append(f"msg={m.group(1)}")
    if msg.raw:
        m = re.search(r"time=([^\s;]+)", msg.raw)
        if m:
            parts.append(f"time={m.group(1)}")
    envelope = " ".join(parts)
    return f"[{envelope}] @{msg.sender}: {msg.content}"


def message_to_dict(msg: Message) -> dict:
    """Convert a Message to a dict for the bridge HTTP API."""
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
        "seq": msg.seq,
        "is_replay": msg.is_replay,
    }


class DaemonHTTPHandler(BaseHTTPRequestHandler):
    """HTTP handler for the daemon's local API, used by the MCP bridge."""

    daemon: "Daemon"  # set by the daemon before starting the server

    def log_message(self, format, *args):
        logger.debug("HTTP: %s", format % args)

    def _respond_json(self, data: dict, status: int = 200):
        body = json.dumps(data).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_body(self) -> dict:
        length = int(self.headers.get("Content-Length", 0))
        if length == 0:
            return {}
        return json.loads(self.rfile.read(length))

    def do_GET(self):
        if self.path == "/messages/pending":
            self._handle_check_messages()
        elif self.path.startswith("/history"):
            self._handle_history()
        elif self.path == "/server/info":
            self._handle_server_info()
        elif self.path.startswith("/tasks"):
            self._handle_list_tasks()
        else:
            self._respond_json({"error": "not found"}, 404)

    def do_POST(self):
        if self.path == "/messages/send":
            self._handle_send_message()
        elif self.path == "/tasks/claim":
            self._handle_task_claim()
        elif self.path == "/tasks/done":
            self._handle_task_done()
        else:
            self._respond_json({"error": "not found"}, 404)

    def _handle_check_messages(self):
        """Drain pending delivery buffer and return messages."""
        messages = []
        with self.daemon.inbox_lock:
            while self.daemon.agent.pending_inbox:
                msg = self.daemon.agent.pending_inbox.popleft()
                messages.append(message_to_dict(msg))
        self._respond_json({"messages": messages})

    def _handle_send_message(self):
        """Queue a PRIVMSG to be sent via IRC."""
        body = self._read_body()
        target = body.get("target", "")
        content = body.get("content", "")
        if not target or not content:
            self._respond_json({"error": "target and content required"}, 400)
            return
        self.daemon.outgoing_queue.append((target, content))
        self._respond_json({"status": "queued"})

    def _handle_history(self):
        """Request CHATHISTORY from IRC server and wait for response."""
        params = urllib.parse.parse_qs(
            urllib.parse.urlparse(self.path).query
        )
        channel = params.get("channel", [""])[0]
        after_seq = int(params.get("after_seq", ["0"])[0])
        limit = int(params.get("limit", ["50"])[0])

        if not channel:
            self._respond_json({"error": "channel required"}, 400)
            return

        # Use sync request to collect CHATHISTORY responses
        loop = self.daemon._loop
        if not loop:
            self._respond_json({"error": "daemon not ready"}, 503)
            return

        future = asyncio.run_coroutine_threadsafe(
            self.daemon._request_history(channel, after_seq, limit), loop
        )
        try:
            messages = future.result(timeout=IRC_REQUEST_TIMEOUT)
            self._respond_json({"messages": messages})
        except Exception as e:
            self._respond_json({"error": str(e)}, 500)

    def _handle_server_info(self):
        """Return known channels and agent info."""
        channels = [{"name": ch, "members": []} for ch in self.daemon.channels]
        self._respond_json({
            "channels": channels,
            "agents": [self.daemon.nick],
        })

    def _handle_list_tasks(self):
        """Request TASK LIST from IRC server and wait for response."""
        params = urllib.parse.parse_qs(
            urllib.parse.urlparse(self.path).query
        )
        channel = params.get("channel", [""])[0]
        if not channel:
            self._respond_json({"error": "channel required"}, 400)
            return

        loop = self.daemon._loop
        if not loop:
            self._respond_json({"error": "daemon not ready"}, 503)
            return

        future = asyncio.run_coroutine_threadsafe(
            self.daemon._request_task_list(channel), loop
        )
        try:
            tasks = future.result(timeout=IRC_REQUEST_TIMEOUT)
            self._respond_json({"tasks": tasks})
        except Exception as e:
            self._respond_json({"error": str(e)}, 500)

    def _handle_task_claim(self):
        body = self._read_body()
        task_id = body.get("task_id", "")
        if not task_id:
            self._respond_json({"error": "task_id required"}, 400)
            return
        self.daemon.task_requests.append(("claim", task_id))
        self._respond_json({"status": "claim requested"})

    def _handle_task_done(self):
        body = self._read_body()
        task_id = body.get("task_id", "")
        if not task_id:
            self._respond_json({"error": "task_id required"}, 400)
            return
        self.daemon.task_requests.append(("done", task_id))
        self._respond_json({"status": "done requested"})


class Daemon:
    """The aircd local runtime wrapper.

    Connects to aircd IRC server, manages a Claude Code process,
    and bridges messages between them.
    """

    def __init__(
        self,
        host: str,
        port: int,
        token: str,
        nick: str,
        channels: list[str],
        http_port: int = 7667,
        claude_model: str = "sonnet",
    ):
        self.host = host
        self.port = port
        self.token = token
        self.nick = nick
        self.channels = channels
        self.http_port = http_port
        self.claude_model = claude_model

        self.irc: Optional[AircdClient] = None
        self.agent = AgentState()
        self.inbox_lock = Lock()
        self.outgoing_queue: deque = deque()
        self.task_requests: deque = deque()

        # Sync request/response slots for history and task list
        self._history_request: Optional[SyncRequest] = None
        self._task_list_request: Optional[SyncRequest] = None

        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._notification_task: Optional[asyncio.Task] = None
        self._shutdown = False
        self._initial_connect = True

    async def run(self):
        """Main daemon loop."""
        self._loop = asyncio.get_event_loop()

        # Start local HTTP API for the MCP bridge
        self._start_http_server()

        # Connect to IRC
        self.irc = AircdClient(
            self.host,
            self.port,
            token=self.token,
            nick=self.nick,
            auto_reconnect=True,
        )
        await self.irc.connect()

        # Join channels
        for ch in self.channels:
            await self.irc.join(ch)
        logger.info("Joined channels: %s", self.channels)

        # Start Claude process
        await self._start_claude()

        # Main event loop
        try:
            await asyncio.gather(
                self._irc_reader_loop(),
                self._outgoing_sender_loop(),
                self._command_processor_loop(),
            )
        except asyncio.CancelledError:
            pass
        finally:
            await self._cleanup()

    def _start_http_server(self):
        """Start the local HTTP server for MCP bridge communication."""
        DaemonHTTPHandler.daemon = self
        server = HTTPServer(("127.0.0.1", self.http_port), DaemonHTTPHandler)
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        logger.info("Daemon HTTP API running on http://127.0.0.1:%d", self.http_port)

    async def _start_claude(self):
        """Spawn the Claude Code CLI process."""
        claude_bin = find_claude_cli()

        # Write MCP config to temp file
        bridge_script = os.path.join(os.path.dirname(__file__), "bridge.py")
        python_bin = sys.executable

        mcp_config = {
            "mcpServers": {
                "chat": {
                    "command": python_bin,
                    "args": [bridge_script],
                    "env": {
                        "AIRCD_DAEMON_URL": f"http://127.0.0.1:{self.http_port}",
                    },
                }
            }
        }

        self._mcp_config_file = tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", prefix="aircd-mcp-", delete=False
        )
        json.dump(mcp_config, self._mcp_config_file)
        self._mcp_config_file.close()

        args = [
            claude_bin,
            "--dangerously-skip-permissions",
            "--input-format", "stream-json",
            "--output-format", "stream-json",
            "--mcp-config", self._mcp_config_file.name,
            "--model", self.claude_model,
        ]

        if self.agent.session_id:
            args.extend(["--resume", self.agent.session_id])

        # Build initial prompt
        channel_list = ", ".join(self.channels)
        initial_prompt = (
            f"You are an AI agent connected to aircd IRC server as '{self.nick}'. "
            f"You are in channels: {channel_list}. "
            f"Use the MCP chat tools (check_messages, send_message, read_history, "
            f"list_tasks, claim_task, complete_task) to interact with the IRC server. "
            f"When you receive a system notification about new messages, call "
            f"check_messages to read them and respond appropriately."
        )

        logger.info("Starting Claude: %s", " ".join(args))
        proc = subprocess.Popen(
            args,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self.agent.process = proc

        # Send initial prompt via stdin
        stdin_msg = encode_stdin_message(initial_prompt, self.agent.session_id)
        if proc.stdin:
            proc.stdin.write((stdin_msg + "\n").encode("utf-8"))
            proc.stdin.flush()

        # Start stdout reader in background
        asyncio.create_task(self._claude_stdout_reader())
        self.agent.is_busy = True

        logger.info("Claude started (PID %d)", proc.pid)

    async def _claude_stdout_reader(self):
        """Read Claude's stream-json stdout and track state."""
        proc = self.agent.process
        if not proc or not proc.stdout:
            return

        loop = asyncio.get_event_loop()
        while not self._shutdown:
            try:
                line = await loop.run_in_executor(None, proc.stdout.readline)
                if not line:
                    logger.info("Claude process exited")
                    self.agent.is_busy = False
                    self.agent.process = None
                    # Auto-restart if there are pending messages
                    with self.inbox_lock:
                        has_pending = bool(self.agent.pending_inbox)
                    if has_pending:
                        logger.info("Restarting Claude to deliver pending messages")
                        await self._start_claude()
                    break

                line_str = line.decode("utf-8", errors="replace").strip()
                if not line_str:
                    continue

                try:
                    event = json.loads(line_str)
                except json.JSONDecodeError:
                    logger.debug("Non-JSON stdout: %s", line_str[:200])
                    continue

                event_type = event.get("type", "")

                if event_type == "system":
                    sid = event.get("session_id")
                    if sid:
                        self.agent.session_id = sid
                        logger.info("Claude session: %s", sid)

                elif event_type == "result":
                    # Turn completed — Claude is now idle
                    self.agent.is_busy = False
                    logger.debug("Claude turn completed, now idle")

                    # Deliver any pending messages
                    with self.inbox_lock:
                        has_pending = bool(self.agent.pending_inbox)
                    if has_pending:
                        await self._deliver_pending_idle()

            except Exception as e:
                logger.error("Error reading Claude stdout: %s", e)
                break

    async def _deliver_pending_idle(self):
        """Deliver pending messages directly via stdin when Claude is idle."""
        proc = self.agent.process
        if not proc or not proc.stdin:
            return

        with self.inbox_lock:
            if not self.agent.pending_inbox:
                return
            messages = []
            while self.agent.pending_inbox:
                messages.append(self.agent.pending_inbox.popleft())

        text = "\n".join(
            f"New message received:\n\n{format_envelope(m)}"
            for m in messages
        )
        text += "\n\nRespond as appropriate. Complete all your work before stopping."

        stdin_msg = encode_stdin_message(text, self.agent.session_id)
        try:
            proc.stdin.write((stdin_msg + "\n").encode("utf-8"))
            proc.stdin.flush()
            self.agent.is_busy = True
            logger.info("Delivered %d pending messages (idle mode)", len(messages))
        except (BrokenPipeError, OSError) as e:
            logger.error("Failed to write to Claude stdin: %s", e)
            with self.inbox_lock:
                for m in reversed(messages):
                    self.agent.pending_inbox.appendleft(m)

    async def _deliver_busy_notification(self):
        """Send a notification to Claude that new messages are waiting."""
        proc = self.agent.process
        if not proc or not proc.stdin or not self.agent.is_busy:
            return

        now = time.time()
        if now - self.agent.last_notification_time < BUSY_NOTIFICATION_DEBOUNCE:
            return

        with self.inbox_lock:
            count = len(self.agent.pending_inbox)
        if count == 0:
            return

        notification = (
            f"[System notification: You have {count} new message(s) waiting. "
            f"Call check_messages to read them when you're ready.]"
        )

        stdin_msg = encode_stdin_message(notification, self.agent.session_id)
        try:
            proc.stdin.write((stdin_msg + "\n").encode("utf-8"))
            proc.stdin.flush()
            self.agent.last_notification_time = now
            logger.debug("Sent busy notification (%d pending)", count)
        except (BrokenPipeError, OSError) as e:
            logger.error("Failed to send notification: %s", e)

    async def _request_history(self, channel: str, after_seq: int, limit: int) -> list:
        """Send CHATHISTORY and collect replay responses synchronously."""
        req = SyncRequest(channel=channel)
        self._history_request = req

        await self.irc.history(channel, after_seq, limit)

        # Wait for responses with timeout — the reader loop will collect
        # replay-tagged messages and signal when done
        try:
            await asyncio.wait_for(req.event.wait(), timeout=IRC_REQUEST_TIMEOUT)
        except asyncio.TimeoutError:
            pass

        self._history_request = None
        return req.responses

    async def _request_task_list(self, channel: str) -> list:
        """Send TASK LIST and collect response NOTICEs synchronously."""
        req = SyncRequest(channel=channel)
        self._task_list_request = req

        await self.irc.task_list(channel)

        try:
            await asyncio.wait_for(req.event.wait(), timeout=IRC_REQUEST_TIMEOUT)
        except asyncio.TimeoutError:
            pass

        self._task_list_request = None
        return req.responses

    async def _irc_reader_loop(self):
        """Read messages from IRC and route to Claude or sync requests."""
        async for msg in self.irc.messages():
            if self._shutdown:
                break

            # Skip our own messages
            if msg.sender == self.nick:
                continue

            # Check if this is a response to a sync history request
            if msg.is_replay and self._history_request:
                req = self._history_request
                if msg.channel == req.channel:
                    req.responses.append(message_to_dict(msg))
                    # Schedule a short delay to collect batch, then signal done
                    if not req.done:
                        req.done = True
                        asyncio.create_task(self._finalize_sync_request(req, 0.5))
                    continue

            # Check if this is a TASK LIST response (NOTICE from server)
            if (self._task_list_request
                    and msg.sender in ("aircd", "server")
                    and "TASK " in msg.content):
                req = self._task_list_request
                # Parse task list line: TASK <id> channel=<ch> status=<s> ...
                task_match = re.match(
                    r"TASK\s+(\S+)\s+channel=(\S+)\s+status=(\S+)\s+"
                    r"claimed_by=(\S+)\s+lease_expires_at=(\S+)\s+title=:(.*)",
                    msg.content,
                )
                if task_match:
                    req.responses.append({
                        "id": task_match.group(1),
                        "channel": task_match.group(2),
                        "status": task_match.group(3),
                        "claimed_by": task_match.group(4),
                        "title": task_match.group(6),
                    })
                    if not req.done:
                        req.done = True
                        asyncio.create_task(self._finalize_sync_request(req, 0.5))
                continue

            # On initial connect, deliver replay messages as context
            if msg.is_replay and self._initial_connect:
                logger.debug("Replay (initial connect): [%s] %s: %s",
                             msg.channel, msg.sender, msg.content[:80])
                with self.inbox_lock:
                    self.agent.pending_inbox.append(msg)
                continue

            # Skip replay messages after initial connect (reconnect catch-up
            # is handled by the bouncer; we don't re-deliver to Claude)
            if msg.is_replay:
                logger.debug("Skipping replay (reconnect): %s", msg.content[:80])
                continue

            # Mark initial connect phase as over once we see a live message
            if self._initial_connect:
                self._initial_connect = False
                # Deliver any collected replay messages
                with self.inbox_lock:
                    has_pending = bool(self.agent.pending_inbox)
                if has_pending and not self.agent.is_busy:
                    await self._deliver_pending_idle()

            # Dedup by msg-id
            msg_id = ""
            if msg.raw:
                m = re.search(r"msg-id=([^\s;]+)", msg.raw)
                if m:
                    msg_id = m.group(1)
            if msg_id and msg_id in self.agent.seen_msg_ids:
                continue
            if msg_id:
                self.agent.seen_msg_ids.add(msg_id)
                if len(self.agent.seen_msg_ids) > 10000:
                    self.agent.seen_msg_ids.clear()

            logger.info("[%s] %s: %s", msg.channel, msg.sender, msg.content[:80])

            if self.agent.is_busy:
                with self.inbox_lock:
                    self.agent.pending_inbox.append(msg)
                    if len(self.agent.pending_inbox) > MAX_PENDING:
                        self.agent.pending_inbox.popleft()
                await self._deliver_busy_notification()
            elif self.agent.process is None:
                # Claude exited — restart with this message
                with self.inbox_lock:
                    self.agent.pending_inbox.append(msg)
                logger.info("Claude not running, restarting to deliver message")
                await self._start_claude()
            else:
                with self.inbox_lock:
                    self.agent.pending_inbox.append(msg)
                await self._deliver_pending_idle()

    async def _finalize_sync_request(self, req: SyncRequest, delay: float):
        """Wait a short time for more responses, then signal done."""
        await asyncio.sleep(delay)
        req.event.set()

    async def _outgoing_sender_loop(self):
        """Send queued outgoing messages via IRC."""
        while not self._shutdown:
            while self.outgoing_queue:
                target, content = self.outgoing_queue.popleft()
                try:
                    await self.irc.privmsg(target, content)
                    logger.info("Sent to %s: %s", target, content[:80])
                except Exception as e:
                    logger.error("Failed to send to %s: %s", target, e)
            await asyncio.sleep(0.1)

    async def _command_processor_loop(self):
        """Process queued IRC commands (task claim/done)."""
        while not self._shutdown:
            while self.task_requests:
                action, arg = self.task_requests.popleft()
                try:
                    if action == "claim":
                        await self.irc.task_claim(arg)
                    elif action == "done":
                        await self.irc.task_done(arg)
                except Exception as e:
                    logger.error("Task %s failed: %s", action, e)

            await asyncio.sleep(0.1)

    async def _cleanup(self):
        """Clean up resources."""
        self._shutdown = True
        if self.agent.process:
            try:
                self.agent.process.terminate()
                self.agent.process.wait(timeout=5)
            except Exception:
                self.agent.process.kill()
        if self.irc:
            await self.irc.close()
        if hasattr(self, "_mcp_config_file"):
            try:
                os.unlink(self._mcp_config_file.name)
            except OSError:
                pass


def main():
    parser = ArgumentParser(description="aircd local runtime wrapper")
    parser.add_argument("--host", default="localhost", help="aircd server host")
    parser.add_argument("--port", type=int, default=6667, help="aircd server port")
    parser.add_argument("--token", required=True, help="Agent authentication token")
    parser.add_argument("--nick", required=True, help="Agent nick")
    parser.add_argument(
        "--channels",
        required=True,
        help="Comma-separated list of channels to join",
    )
    parser.add_argument(
        "--http-port",
        type=int,
        default=7667,
        help="Local HTTP port for MCP bridge (default: 7667)",
    )
    parser.add_argument(
        "--model",
        default="sonnet",
        help="Claude model to use (default: sonnet)",
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Enable verbose logging",
    )

    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )

    channels = [ch.strip() for ch in args.channels.split(",") if ch.strip()]

    daemon = Daemon(
        host=args.host,
        port=args.port,
        token=args.token,
        nick=args.nick,
        channels=channels,
        http_port=args.http_port,
        claude_model=args.model,
    )

    loop = asyncio.new_event_loop()

    def handle_signal(sig, frame):
        logger.info("Shutting down...")
        daemon._shutdown = True
        for task in asyncio.all_tasks(loop):
            task.cancel()

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    try:
        loop.run_until_complete(daemon.run())
    except KeyboardInterrupt:
        pass
    finally:
        loop.close()


if __name__ == "__main__":
    main()
