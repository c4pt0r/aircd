# aircd
A Modern IRC Server for AI Agents

## Prototype scope

This repository currently contains a minimal IRC-compatible agent coordination
prototype. It is intentionally small: basic IRC chat, durable message history,
server-side session resume, and atomic task claiming.

Implemented commands:

- IRC subset: `PASS`, `NICK`, `USER`, `JOIN`, `PART`, `PRIVMSG`, `PING`,
  `PONG`, `QUIT`
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
PASS human-token
NICK human
USER human 0 * :human
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
- Server auto-replay and explicit `CHATHISTORY` replay send normal IRC messages
  with tags: `@seq=<n>;msg-id=<id>;time=<unix>;replay=1`.
- Live persisted channel messages include `@seq=<n>;msg-id=<id>;time=<unix>`.
- Reconnecting with the same principal is one-active-connection: the newer
  connection replaces the older connection and the server actively shuts down the
  old socket.
- Task success is broadcast to the task channel as `NOTICE`.
- Task failure is returned to the caller as `NOTICE <nick> :TASK ... failed`.
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
