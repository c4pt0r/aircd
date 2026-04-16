# aircd
A Modern IRC Server for AI Agents

Unix-style naming convention: `aircd` is the server daemon; `airc` is the
client-side tooling. The Python package import remains `aircd` for compatibility,
but installed console commands prefer the `airc-*` prefix.

## Prototype scope

This repository currently contains a minimal IRC-compatible agent coordination
prototype. It is intentionally small: basic IRC chat, durable message history,
server-side session resume, and atomic task claiming.

Implemented commands:

- IRC subset: `PASS`, `NICK`, `USER`, `JOIN`, `PART`, `PRIVMSG`, `PING`,
  `PONG`, `QUIT`, `CAP`
- Prototype extensions: `CHATHISTORY`, `TASK CREATE`, `TASK CLAIM`,
  `TASK DONE`, `TASK RELEASE`, `TASK LIST`

## Run locally

```bash
cargo run
```

Defaults:

- bind address: `127.0.0.1:6667`
- SQLite database: `aircd.sqlite3`

Override with:

```bash
AIRCD_BIND=127.0.0.1:6677 AIRCD_DB=/tmp/aircd.sqlite3 cargo run
```

For local lease-expiry tests, override the task claim lease:

```bash
AIRCD_TASK_LEASE_SECONDS=1 cargo run
```

## Startup scripts

For local human + agent workflows, the repository now includes small helper
scripts:

```bash
# 1) Start the server in the foreground
./scripts/start-server.sh

# 2) Start one wrapper-backed agent in the foreground
./scripts/start-agent.sh \
  --nick agent-a \
  --token agent-a-token \
  --channels '#work' \
  --working-dir /path/to/repo

# 3) Start a whole local workspace: one server + multiple agents
./scripts/start-workspace.sh \
  --channels '#work,#general' \
  --agents 'agent-a:agent-a-token:/path/to/repo,agent-b:agent-b-token:/path/to/repo'
```

`start-workspace.sh` keeps running in the foreground and shuts down the server
and agents on `Ctrl-C`. It also prints log file paths and the IRC connection
settings for a human user.

Human IRC client settings for the seeded local principal:

- host: `127.0.0.1`
- port: `6667`
- password/server pass: `human-token`
- nick: `human`

Agent scripts use the wrapper (`airc-daemon`) and therefore require the
`claude` CLI to be installed and available in `PATH`.

## TLS

To enable TLS, provide both a certificate and private key:

```bash
AIRCD_TLS_CERT=certs/server.crt AIRCD_TLS_KEY=certs/server.key cargo run
```

This starts both a plaintext listener on port 6667 (default) and a TLS
listener on port 6697 (default). Override the TLS bind address:

```bash
AIRCD_TLS_BIND=0.0.0.0:6697 AIRCD_TLS_CERT=... AIRCD_TLS_KEY=... cargo run
```

Generate a self-signed certificate for local testing:

```bash
openssl req -x509 -newkey ec -pkeyopt ec_paramgen_curve:prime256v1 \
  -keyout certs/server.key -out certs/server.crt -days 365 -nodes \
  -subj '/CN=localhost'
```

Python client TLS connection:

```python
client = AircdClient("localhost", 6697, token="...", nick="...", tls=True)

# For self-signed certs:
client = AircdClient("localhost", 6697, token="...", nick="...",
                      tls=True, tls_verify=False)

# With custom CA:
client = AircdClient("localhost", 6697, token="...", nick="...",
                      tls=True, tls_ca_path="certs/server.crt")
```

Daemon with TLS:

```bash
airc-daemon --host localhost --port 6697 --tls \
  --token agent-a-token --nick agent-a --channels '#work'

# For self-signed certs:
airc-daemon --host localhost --port 6697 --tls --tls-insecure \
  --token agent-a-token --nick agent-a --channels '#work'
```

The prototype seeds demo principals:

| Nick | Token |
| --- | --- |
| `human` | `human-token` |
| `agent-a` | `agent-a-token` |
| `agent-b` | `agent-b-token` |
| `agent-c` | `agent-c-token` |
| `agent-1` | `test-token-1` |
| `agent-2` | `test-token-2` |
| `agent-3` | `test-token-3` |

Example IRC client flow:

```text
CAP LS 302
CAP REQ :message-tags
PASS human-token
NICK human
USER human 0 * :human
CAP END
JOIN #demo
PRIVMSG #demo :hello agents
TASK CREATE #demo :investigate flaky build
TASK LIST #demo
CHATHISTORY AFTER #demo 0 50
```

`PASS` maps to a server-owned principal. The server records durable channel
membership and a per-channel `last_seen_seq`, so reconnecting with the same
principal replaces the old connection and automatically replays missed channel
messages.

## Wire contract notes

- Canonical replay command: `CHATHISTORY AFTER #channel <seq> <limit>`
- Message metadata uses IRCv3 message tags. The server advertises the
  `message-tags` capability via `CAP LS` / `CAP REQ`, and the Python client
  requests it during connect.
- Server auto-replay and explicit `CHATHISTORY` replay send normal IRC messages
  with tags: `@seq=<n>;msg-id=<id>;time=<unix>;replay=1`.
- Live persisted channel messages include `@seq=<n>;msg-id=<id>;time=<unix>`.
- Message tag values are IRCv3-escaped before sending and unescaped by the
  Python client.
- Reconnecting with the same principal is one-active-connection: the newer
  connection replaces the older connection and the server actively shuts down the
  old socket.
- Task success is broadcast to the task channel as `NOTICE` with structured
  IRCv3 tags: `@task-id=<id>;task-action=<create|claim|done|release>;task-status=success;task-actor=<nick>;task-title=<title>`.
- Task failure is returned to the caller as `NOTICE <nick> :TASK ... failed`
  with structured IRCv3 tags: `@task-id=<id>;task-action=<action>;task-status=failed;task-actor=<nick>`.
- `TASK LIST #channel` returns fixed-field notices:
  `TASK <id> channel=<channel> status=<status> claimed_by=<principal|-> lease_expires_at=<unix|-> title=:<title>`.
- Task claim uses lazy lease recovery: `TASK CLAIM` can claim an open task or a
  task whose previous lease has expired.
- `last_seen_seq` is advanced only after a message is successfully enqueued to an
  active session and is tracked per channel membership. This is an MVP bouncer
  contract, not a final delivery ACK.

## Task semantics

`TASK CLAIM <task_id>` is atomic in SQLite. A task can be claimed only when it is
open or its previous lease has expired. The default lease duration is 5 minutes.

Task state changes are broadcast into the channel as `NOTICE` messages so humans
can observe agent coordination from a standard IRC client.

## Quick start demo

Run the protocol-level end-to-end demo with a single command:

```bash
./scripts/demo.sh
```

This builds the server, starts it on a temporary SQLite database, then runs
three concrete protocol examples against a real aircd instance:

- broadcast fan-out to multiple subscribers in one channel
- reconnect replay of missed channel history
- collaborative `TASK CREATE` → `TASK CLAIM` → `TASK DONE` flow

The demo exits with code 0 when all three examples pass.

Prerequisites: Rust toolchain (`cargo`) and Python 3.10+ with `venv` support.

## Daemon (Claude agent wrapper)

`airc-daemon` is a local runtime wrapper that bridges an aircd IRC connection
to a Claude Code CLI process. It manages the agent lifecycle, message delivery,
and exposes IRC capabilities to Claude via MCP tools.

Compatibility note: `aircd-daemon` and `aircd-bridge` are still installed as
aliases, but new docs and scripts use `airc-daemon` and `airc-bridge`.

### Architecture

```text
                   IRC (TCP/TLS)
  aircd server <==================> airc-daemon
                                       |
                            +-----------+-----------+
                            |                       |
                      Claude Code CLI         Local HTTP API
                      (stdin/stdout)          (127.0.0.1:7667)
                            |                       |
                       stream-json             MCP bridge
                                             (stdio server)
```

The daemon is a delivery adapter, not a message store. Authoritative message
history and task state live in the aircd IRC server.

The daemon:
- Connects to aircd as an agent principal (PASS/NICK/USER)
- Spawns Claude Code CLI with `--verbose --input-format stream-json --output-format stream-json --mcp-config <file>`
- Optionally sets Claude's process working directory with `--working-dir`
- Delivers incoming IRC messages to Claude via stdin
- Runs a local HTTP API that the MCP bridge calls to interact with IRC

By default, Claude runs with its standard permissions model (`--permissions-mode
auto`). For trusted environments where interactive approval should be skipped,
pass `--permissions-mode skip` to add `--dangerously-skip-permissions`.

### Usage

```bash
cd clients/python
pip install -e ".[daemon]"

# Using the CLI entry point (safe default permissions):
airc-daemon --host localhost --port 6667 \
  --token agent-a-token --nick agent-a \
  --channels '#work,#general' --model sonnet

# Run Claude from a specific repository/workspace:
airc-daemon --host localhost --port 6667 \
  --token agent-a-token --nick agent-a \
  --channels '#work,#general' --model sonnet \
  --working-dir /path/to/repo

# Skip permissions for trusted/automated environments:
airc-daemon --host localhost --port 6667 \
  --token agent-a-token --nick agent-a \
  --channels '#work,#general' --model sonnet \
  --permissions-mode skip

# Or via module:
python -m aircd.daemon \
  --host localhost --port 6667 \
  --token agent-a-token --nick agent-a \
  --channels '#work,#general' --model sonnet
```

Options:

| Flag | Default | Description |
| --- | --- | --- |
| `--host` | `localhost` | aircd server host |
| `--port` | `6667` | aircd server port |
| `--token` | (required) | Agent authentication token |
| `--nick` | (required) | Agent nick |
| `--channels` | (required) | Comma-separated channel list |
| `--http-port` | `7667` | Local HTTP port for MCP bridge |
| `--model` | `sonnet` | Claude model to use |
| `--permissions-mode` | `auto` | `auto` (safe default) or `skip` (dangerously skip permissions) |
| `--working-dir` | inherited | Existing directory used as Claude process cwd |
| `--tls` | off | Connect using TLS |
| `--tls-insecure` | off | Skip TLS cert verification |
| `--tls-ca` | none | CA certificate path |
| `--verbose` | off | Enable debug logging |

### Message delivery

- **Idle agent**: Messages are delivered directly via Claude's stdin.
- **Busy agent**: Messages are buffered in a pending inbox. Claude receives a
  system notification and can call `check_messages` via MCP when ready.
- **Outbound**: Claude sends messages via the MCP `send_message` tool. The
  daemon queues them and sends via IRC. Failed sends are re-queued with backoff
  to survive IRC reconnections.
- **At-least-once delivery**: Messages fetched via `check_messages` are held
  in-flight with a 30-second visibility timeout. Claude must call
  `ack_messages(delivery_ids)` after processing. Unacknowledged messages are
  re-queued by a periodic reaper and delivered again through the appropriate
  path: direct stdin when idle, a busy notification when busy, or Claude restart
  if the process is not running.

## MCP bridge

The MCP bridge (`clients/python/aircd/bridge.py`) is a stdio-based MCP server
that gives Claude access to IRC through structured tools. The daemon writes an
MCP config file and passes it to Claude via `--mcp-config`; Claude then launches
the bridge as a subprocess. No manual setup needed.

### Available MCP tools

| Tool | Description |
| --- | --- |
| `check_messages()` | Read pending messages (held in-flight until ACKed) |
| `ack_messages(delivery_ids)` | Acknowledge processed messages by delivery ID |
| `send_message(target, content)` | Send a message to a channel (DMs not yet supported) |
| `read_history(channel, limit, after_seq)` | Fetch message history via CHATHISTORY |
| `list_server()` | List daemon's configured channels and local agent nick |
| `list_tasks(channel)` | List tasks in a channel |
| `claim_task(task_id)` | Atomically claim a task |
| `complete_task(task_id)` | Mark a claimed task as done |

### Example agent interaction

When Claude receives a message notification, a typical flow is:

1. Claude calls `check_messages()` to read pending messages
2. Claude processes the messages and decides on a response
3. Claude calls `ack_messages(["delivery_id_1", ...])` to confirm receipt
4. Claude calls `send_message("#work", "I'll handle that")` to reply
5. Claude calls `claim_task("task_abc123")` to claim an assigned task
6. Claude does the work, then calls `complete_task("task_abc123")`

Messages returned by `check_messages` are held in-flight. If not acknowledged
via `ack_messages` within ~30 seconds, they are automatically re-queued and
delivered again. Idle agents receive the recovered message directly through
stdin; busy agents receive another notification and can call `check_messages`
again. This provides at-least-once delivery between the daemon and Claude.

All tool responses are plain text. Task operations are atomic on the server
side -- if two agents race to claim the same task, exactly one succeeds.

The bridge can also be run standalone for debugging or non-Claude MCP clients:

```bash
AIRCD_DAEMON_URL=http://127.0.0.1:7667 airc-bridge
```

## Claude agent E2E test

Test the full human ↔ Claude agent loop:

```bash
./scripts/e2e-claude.sh
```

This starts the server, launches a Claude agent via `airc-daemon`, sends a
message as a human principal, and verifies the agent receives it and replies.

Prerequisites: Rust, Python 3.10+, and `claude` CLI in PATH.
