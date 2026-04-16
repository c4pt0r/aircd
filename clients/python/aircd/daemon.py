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
from collections import OrderedDict, deque
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

# Watchdog: max seconds a Claude turn can run before force-idle + restart
TURN_WATCHDOG_TIMEOUT = 600.0  # 10 minutes

# Watchdog: how often to check for stuck turns (seconds)
TURN_WATCHDOG_INTERVAL = 30.0

# Maximum dedup entries before oldest are evicted (LRU bounded)
MAX_DEDUP_ENTRIES = 10000

# How long delivered messages stay in-flight before becoming visible again (seconds)
MESSAGE_VISIBILITY_TIMEOUT = 30.0


@dataclass
class AgentState:
    """Tracks the state of the managed Claude process."""

    process: Optional[subprocess.Popen] = None
    session_id: Optional[str] = None
    is_busy: bool = False
    pending_inbox: deque = field(default_factory=deque)
    in_flight: dict = field(default_factory=dict)  # msg_id -> (Message, delivered_at)
    last_notification_time: float = 0.0
    seen_msg_ids: OrderedDict = field(default_factory=OrderedDict)
    last_stdout_activity: float = 0.0  # monotonic time of last stdout event


@dataclass
class SyncRequest:
    """A synchronous request waiting for IRC responses."""

    event: asyncio.Event = field(default_factory=asyncio.Event)
    responses: list = field(default_factory=list)
    channel: str = ""
    task_id: str = ""
    action: str = ""  # "claim" or "done" — for task action matching
    done: bool = False


def find_claude_cli() -> str:
    """Find the local claude CLI binary."""
    path = shutil.which("claude")
    if path:
        return path
    raise FileNotFoundError(
        "claude CLI not found in PATH. Install Claude Code first."
    )


def resolve_working_dir(path: Optional[str]) -> Optional[str]:
    """Resolve and validate the working directory for the Claude process."""
    if not path:
        return None

    resolved = os.path.abspath(os.path.expanduser(path))
    if not os.path.isdir(resolved):
        raise ValueError(f"working directory does not exist: {path}")
    return resolved


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
        elif self.path == "/messages/ack":
            self._handle_ack_messages()
        elif self.path == "/tasks/claim":
            self._handle_task_claim()
        elif self.path == "/tasks/done":
            self._handle_task_done()
        else:
            self._respond_json({"error": "not found"}, 404)

    def _handle_check_messages(self):
        """Return pending messages with visibility-timeout semantics.

        Messages are moved to an in-flight buffer. If not ACKed within
        MESSAGE_VISIBILITY_TIMEOUT, they become visible again. This gives
        at-least-once delivery between daemon and Claude.
        """
        now = time.time()
        messages = []
        with self.daemon.inbox_lock:
            self.daemon._requeue_expired_in_flight_locked(now, "check_messages")

            # Deliver pending messages → in-flight
            while self.daemon.agent.pending_inbox:
                msg = self.daemon.agent.pending_inbox.popleft()
                msg_dict = message_to_dict(msg)
                mid = msg_dict.get("msg_id") or f"noid_{id(msg)}"
                self.daemon.agent.in_flight[mid] = (msg, now)
                # Expose the delivery_id so the bridge can ACK all messages,
                # including those without a server-assigned msg-id.
                msg_dict["delivery_id"] = mid
                messages.append(msg_dict)
        self._respond_json({"messages": messages})

    def _handle_ack_messages(self):
        """Acknowledge receipt of messages, removing them from in-flight."""
        body = self._read_body()
        msg_ids = body.get("msg_ids", [])
        if not msg_ids:
            self._respond_json({"error": "msg_ids required"}, 400)
            return
        acked = 0
        with self.daemon.inbox_lock:
            for mid in msg_ids:
                if mid in self.daemon.agent.in_flight:
                    del self.daemon.agent.in_flight[mid]
                    acked += 1
        self._respond_json({"acked": acked})

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
        loop = self.daemon._loop
        if not loop:
            self._respond_json({"error": "daemon not ready"}, 503)
            return
        future = asyncio.run_coroutine_threadsafe(
            self.daemon._request_task_action("claim", task_id), loop
        )
        try:
            result = future.result(timeout=IRC_REQUEST_TIMEOUT)
            self._respond_json(result)
        except Exception as e:
            self._respond_json({"error": str(e)}, 500)

    def _handle_task_done(self):
        body = self._read_body()
        task_id = body.get("task_id", "")
        if not task_id:
            self._respond_json({"error": "task_id required"}, 400)
            return
        loop = self.daemon._loop
        if not loop:
            self._respond_json({"error": "daemon not ready"}, 503)
            return
        future = asyncio.run_coroutine_threadsafe(
            self.daemon._request_task_action("done", task_id), loop
        )
        try:
            result = future.result(timeout=IRC_REQUEST_TIMEOUT)
            self._respond_json(result)
        except Exception as e:
            self._respond_json({"error": str(e)}, 500)


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
        permissions_mode: str = "auto",
        working_dir: Optional[str] = None,
        tls: bool = False,
        tls_verify: bool = True,
        tls_ca_path: Optional[str] = None,
    ):
        self.host = host
        self.port = port
        self.token = token
        self.nick = nick
        self.channels = channels
        self.http_port = http_port
        self.claude_model = claude_model
        self.permissions_mode = permissions_mode
        self.working_dir = resolve_working_dir(working_dir)
        self.tls = tls
        self.tls_verify = tls_verify
        self.tls_ca_path = tls_ca_path

        self.irc: Optional[AircdClient] = None
        self.agent = AgentState()
        self.inbox_lock = Lock()
        self.outgoing_queue: deque = deque()
        self._http_server: Optional[HTTPServer] = None
        self._http_thread: Optional[Thread] = None

        # Sync request/response maps keyed by correlation ID
        self._history_requests: dict[str, SyncRequest] = {}
        self._task_list_requests: dict[str, SyncRequest] = {}
        self._task_action_requests: dict[str, SyncRequest] = {}

        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._notification_task: Optional[asyncio.Task] = None
        self._shutdown = False

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
            tls=self.tls,
            tls_verify=self.tls_verify,
            tls_ca_path=self.tls_ca_path,
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
                self._turn_watchdog(),
                self._in_flight_reaper(),
            )
        except asyncio.CancelledError:
            pass
        finally:
            await self._cleanup()

    def _start_http_server(self):
        """Start the local HTTP server for MCP bridge communication."""
        if self._http_server is not None:
            raise RuntimeError("daemon HTTP server is already running")

        DaemonHTTPHandler.daemon = self
        server = HTTPServer(("127.0.0.1", self.http_port), DaemonHTTPHandler)
        thread = Thread(
            target=server.serve_forever,
            name=f"aircd-daemon-http-{self.http_port}",
            daemon=True,
        )
        self._http_server = server
        self._http_thread = thread
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
            "--verbose",
            "--input-format", "stream-json",
            "--output-format", "stream-json",
            "--mcp-config", self._mcp_config_file.name,
            "--model", self.claude_model,
        ]

        if self.permissions_mode == "skip":
            args.insert(1, "--dangerously-skip-permissions")

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
        if self.working_dir:
            initial_prompt += f" Your working directory is {self.working_dir}."

        logger.info("Starting Claude: %s", " ".join(args))
        if self.working_dir:
            logger.info("Claude working directory: %s", self.working_dir)
        proc = subprocess.Popen(
            args,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=self.working_dir,
        )
        self.agent.process = proc

        # Send initial prompt via stdin
        stdin_msg = encode_stdin_message(initial_prompt, self.agent.session_id)
        if proc.stdin:
            proc.stdin.write((stdin_msg + "\n").encode("utf-8"))
            proc.stdin.flush()

        # Start stdout and stderr readers in background
        asyncio.create_task(self._claude_stdout_reader())
        asyncio.create_task(self._claude_stderr_reader())
        self.agent.is_busy = True
        self.agent.last_stdout_activity = time.monotonic()

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
                    # Ignore stale reader tasks from an older Claude process.
                    # The watchdog can replace self.agent.process while this
                    # reader is still draining EOF from the terminated process.
                    if self.agent.process is proc:
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
                self.agent.last_stdout_activity = time.monotonic()

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

    async def _claude_stderr_reader(self):
        """Read Claude's stderr and log it for diagnostics."""
        proc = self.agent.process
        if not proc or not proc.stderr:
            return

        loop = asyncio.get_event_loop()
        while not self._shutdown:
            try:
                line = await loop.run_in_executor(None, proc.stderr.readline)
                if not line:
                    break
                line_str = line.decode("utf-8", errors="replace").strip()
                if line_str:
                    logger.warning("Claude stderr: %s", line_str)
            except Exception:
                break

    async def _turn_watchdog(self):
        """Periodic watchdog that detects stuck Claude turns.

        If Claude is busy and no stdout activity has been seen for
        TURN_WATCHDOG_TIMEOUT seconds, force-transition to idle and
        restart the process. This covers both missing 'result' events
        and functionally stuck (alive but unresponsive) processes.
        """
        while not self._shutdown:
            await asyncio.sleep(TURN_WATCHDOG_INTERVAL)
            if not self.agent.is_busy or not self.agent.process:
                continue
            elapsed = time.monotonic() - self.agent.last_stdout_activity
            if elapsed < TURN_WATCHDOG_TIMEOUT:
                continue
            logger.warning(
                "Watchdog: Claude busy for %.0fs with no stdout activity "
                "(threshold %.0fs). Force-restarting.",
                elapsed,
                TURN_WATCHDOG_TIMEOUT,
            )
            # Force-kill the stuck process
            proc = self.agent.process
            if proc:
                try:
                    proc.terminate()
                    proc.wait(timeout=5)
                except Exception:
                    proc.kill()
            self.agent.is_busy = False
            self.agent.process = None
            # Restart if there are pending messages
            with self.inbox_lock:
                has_pending = bool(self.agent.pending_inbox)
            if has_pending:
                logger.info("Watchdog: restarting Claude to deliver pending messages")
                await self._start_claude()

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
            self.agent.last_stdout_activity = time.monotonic()
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
        key = channel
        if key in self._history_requests:
            raise ValueError(f"history request already in-flight for {key}")
        req = SyncRequest(channel=channel)
        self._history_requests[key] = req

        await self.irc.history(channel, after_seq, limit)

        try:
            await asyncio.wait_for(req.event.wait(), timeout=IRC_REQUEST_TIMEOUT)
        except asyncio.TimeoutError:
            pass

        self._history_requests.pop(key, None)
        return req.responses

    async def _request_task_list(self, channel: str) -> list:
        """Send TASK LIST and collect response NOTICEs synchronously."""
        key = channel
        if key in self._task_list_requests:
            raise ValueError(f"task list request already in-flight for {key}")
        req = SyncRequest(channel=channel)
        self._task_list_requests[key] = req

        await self.irc.task_list(channel)

        try:
            await asyncio.wait_for(req.event.wait(), timeout=IRC_REQUEST_TIMEOUT)
        except asyncio.TimeoutError:
            pass

        self._task_list_requests.pop(key, None)
        return req.responses

    async def _request_task_action(self, action: str, task_id: str) -> dict:
        """Send TASK CLAIM/DONE and wait for server success/failure NOTICE."""
        key = f"{action}:{task_id}"
        if key in self._task_action_requests:
            raise ValueError(f"task action request already in-flight for {key}")
        req = SyncRequest(task_id=task_id, action=action)
        self._task_action_requests[key] = req

        if action == "claim":
            await self.irc.task_claim(task_id)
        elif action == "done":
            await self.irc.task_done(task_id)

        try:
            await asyncio.wait_for(req.event.wait(), timeout=IRC_REQUEST_TIMEOUT)
        except asyncio.TimeoutError:
            self._task_action_requests.pop(key, None)
            return {"status": "timeout", "error": "no response from server"}

        self._task_action_requests.pop(key, None)
        if req.responses:
            return req.responses[0]
        return {"status": "unknown"}

    async def _irc_reader_loop(self):
        """Read messages from IRC and route to Claude or sync requests."""
        async for msg in self.irc.messages():
            if self._shutdown:
                break

            # --- Sync request handlers first (before self-message filter) ---
            # These need to see ALL messages including our own history.

            # Response to sync CHATHISTORY request
            if msg.is_replay and msg.channel in self._history_requests:
                req = self._history_requests[msg.channel]
                req.responses.append(message_to_dict(msg))
                if not req.done:
                    req.done = True
                    asyncio.create_task(self._finalize_sync_request(req, 0.5))
                continue

            # Response to TASK LIST request (NOTICE from server)
            if (self._task_list_requests
                    and msg.sender in ("aircd", "server")
                    and "TASK " in msg.content):
                task_match = re.match(
                    r"TASK\s+(\S+)\s+channel=(\S+)\s+status=(\S+)\s+"
                    r"claimed_by=(\S+)\s+lease_expires_at=(\S+)\s+title=:(.*)",
                    msg.content,
                )
                if task_match:
                    task_channel = task_match.group(2)
                    req = self._task_list_requests.get(task_channel)
                    if req:
                        req.responses.append({
                            "id": task_match.group(1),
                            "channel": task_channel,
                            "status": task_match.group(3),
                            "claimed_by": task_match.group(4),
                            "title": task_match.group(6),
                        })
                        if not req.done:
                            req.done = True
                            asyncio.create_task(self._finalize_sync_request(req, 0.5))
                continue

            # Response to TASK CLAIM/DONE (NOTICE with structured tags or body)
            if (self._task_action_requests
                    and msg.sender in ("aircd", "server")):
                # Prefer structured tags if available
                tag_task_id = msg.tags.get("task-id", "")
                tag_action = msg.tags.get("task-action", "")
                tag_status = msg.tags.get("task-status", "")
                tag_actor = msg.tags.get("task-actor", "")
                if tag_task_id and tag_action and tag_status:
                    key = f"{tag_action}:{tag_task_id}"
                    req = self._task_action_requests.get(key)
                    if req:
                        if tag_status == "failed":
                            req.responses.append({"status": "failed", "error": msg.content})
                            req.event.set()
                            continue
                        elif tag_status == "success" and tag_actor == self.nick:
                            status = "claimed" if tag_action == "claim" else "done"
                            req.responses.append({"status": status, "detail": msg.content})
                            req.event.set()
                            continue
                        elif tag_status == "success" and tag_actor != self.nick:
                            pass  # Another agent's broadcast — keep waiting
                else:
                    # Fallback: parse body with regex
                    fail_match = re.match(
                        r"TASK\s+(CLAIM|DONE)\s+(\S+)\s+failed[\s:]",
                        msg.content,
                    )
                    if fail_match:
                        fail_action = "claim" if fail_match.group(1) == "CLAIM" else "done"
                        key = f"{fail_action}:{fail_match.group(2)}"
                        req = self._task_action_requests.get(key)
                        if req:
                            req.responses.append({"status": "failed", "error": msg.content})
                            req.event.set()
                            continue
                    success_match = re.match(
                        r"TASK\s+(\S+)\s+(claimed|completed)\s+by\s+(\S+?):",
                        msg.content,
                    )
                    if success_match:
                        event_action = "claim" if success_match.group(2) == "claimed" else "done"
                        who = success_match.group(3)
                        key = f"{event_action}:{success_match.group(1)}"
                        req = self._task_action_requests.get(key)
                        if req and who == self.nick:
                            status = "claimed" if event_action == "claim" else "done"
                            req.responses.append({"status": status, "detail": msg.content})
                            req.event.set()
                            continue

            # --- Now filter self-messages for normal delivery ---
            if msg.sender == self.nick:
                continue

            # All replay messages (bouncer catch-up on connect or reconnect)
            # go through normal dedup + delivery. This ensures the wrapper
            # never drops messages that arrived while disconnected.
            # Only explicit _history_request replays are consumed above.

            # Dedup by msg-id
            msg_id = ""
            if msg.raw:
                m = re.search(r"msg-id=([^\s;]+)", msg.raw)
                if m:
                    msg_id = m.group(1)
            if msg_id and msg_id in self.agent.seen_msg_ids:
                self.agent.seen_msg_ids.move_to_end(msg_id)
                continue
            if msg_id:
                self.agent.seen_msg_ids[msg_id] = True
                while len(self.agent.seen_msg_ids) > MAX_DEDUP_ENTRIES:
                    self.agent.seen_msg_ids.popitem(last=False)

            logger.info("[%s] %s: %s", msg.channel, msg.sender, msg.content[:80])

            if self.agent.is_busy:
                with self.inbox_lock:
                    self.agent.pending_inbox.append(msg)
                    if len(self.agent.pending_inbox) > MAX_PENDING:
                        self.agent.pending_inbox.popleft()
                await self._deliver_busy_notification()
            elif self.agent.process is None:
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
        """Send queued outgoing messages via IRC.

        On send failure (e.g. disconnected writer during reconnect), the
        message is re-queued at the front and we back off to let the IRC
        client reconnect before retrying.
        """
        while not self._shutdown:
            while self.outgoing_queue:
                target, content = self.outgoing_queue.popleft()
                try:
                    await self.irc.privmsg(target, content)
                    logger.info("Sent to %s: %s", target, content[:80])
                except Exception as e:
                    logger.warning(
                        "Send to %s failed (will retry): %s", target, e
                    )
                    self.outgoing_queue.appendleft((target, content))
                    # Back off to let the IRC client reconnect
                    await asyncio.sleep(2.0)
                    break  # restart the outer loop
            await asyncio.sleep(0.1)

    def _requeue_expired_in_flight_locked(self, now: float, source: str) -> int:
        """Move expired in-flight messages back to pending.

        Caller must hold inbox_lock.
        """
        expired = [
            (mid, msg)
            for mid, (msg, delivered_at) in self.agent.in_flight.items()
            if now - delivered_at > MESSAGE_VISIBILITY_TIMEOUT
        ]
        for mid, msg in expired:
            del self.agent.in_flight[mid]
            self.agent.pending_inbox.appendleft(msg)
            logger.warning("%s: re-queued unacked message %s", source, mid)
        return len(expired)

    async def _in_flight_reaper(self):
        """Periodically reap expired in-flight messages back to pending.

        This ensures messages are recovered even if Claude never calls
        check_messages again (e.g. after a crash or stuck turn).
        """
        while not self._shutdown:
            await asyncio.sleep(MESSAGE_VISIBILITY_TIMEOUT / 2)
            now = time.time()
            with self.inbox_lock:
                requeued = self._requeue_expired_in_flight_locked(now, "reaper")
            if requeued == 0:
                continue

            await self._wake_pending_delivery("reaper")

    async def _wake_pending_delivery(self, source: str):
        """Trigger delivery after pending messages become available."""
        if self.agent.process is None:
            logger.info("%s: restarting Claude to deliver pending messages", source)
            await self._start_claude()
        elif self.agent.is_busy:
            await self._deliver_busy_notification()
        else:
            await self._deliver_pending_idle()

    async def _cleanup(self):
        """Clean up resources."""
        self._shutdown = True
        await asyncio.to_thread(self._shutdown_http_server)
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

    def _shutdown_http_server(self):
        """Stop the local HTTP bridge server and close its listening socket."""
        server = self._http_server
        thread = self._http_thread
        self._http_server = None
        self._http_thread = None

        if server is None:
            return

        try:
            logger.info("Shutting down daemon HTTP API")
            if thread and thread.is_alive():
                server.shutdown()
                thread.join(timeout=2.0)
                if thread.is_alive():
                    logger.warning("Daemon HTTP thread did not stop within timeout")
        finally:
            server.server_close()


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
        "--permissions-mode",
        choices=["auto", "skip"],
        default="auto",
        help=(
            "Claude Code permissions mode. 'auto' uses default safe permissions; "
            "'skip' passes --dangerously-skip-permissions for trusted environments "
            "(default: auto)"
        ),
    )
    parser.add_argument(
        "--working-dir",
        default=None,
        help=(
            "Working directory for the Claude Code process. Relative paths are "
            "resolved from the daemon's current directory. Defaults to inheriting "
            "the daemon working directory."
        ),
    )
    parser.add_argument(
        "--tls",
        action="store_true",
        help="Connect to server using TLS",
    )
    parser.add_argument(
        "--tls-insecure",
        action="store_true",
        help="Disable TLS certificate verification (for self-signed certs)",
    )
    parser.add_argument(
        "--tls-ca",
        default=None,
        help="Path to CA certificate file for TLS verification",
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

    try:
        daemon = Daemon(
            host=args.host,
            port=args.port,
            token=args.token,
            nick=args.nick,
            channels=channels,
            http_port=args.http_port,
            claude_model=args.model,
            permissions_mode=args.permissions_mode,
            working_dir=args.working_dir,
            tls=args.tls,
            tls_verify=not args.tls_insecure,
            tls_ca_path=args.tls_ca,
        )
    except ValueError as e:
        parser.error(str(e))

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
