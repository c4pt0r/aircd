#!/usr/bin/env bash
#
# Start a local aircd workspace: one server for human IRC clients and a set of
# wrapper-backed agents joined to the requested channels.
#
# Usage:
#   ./scripts/start-workspace.sh
#   ./scripts/start-workspace.sh --channels '#work,#general' --agents 'agent-a:agent-a-token:/repo,agent-b:agent-b-token:/repo'

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
CALLER_DIR="$(pwd)"
STATE_DIR="$REPO_ROOT/.aircd/workspace"
LOG_DIR="$STATE_DIR/logs"

BIND_ADDR="${AIRCD_BIND:-127.0.0.1:6667}"
DB_PATH="${AIRCD_DB:-$STATE_DIR/local.sqlite3}"
CHANNELS="${AIRCD_CHANNELS:-#work}"
MODEL="${CLAUDE_MODEL:-sonnet}"
PERMISSIONS_MODE="${AIRCD_PERMISSIONS_MODE:-skip}"
HTTP_PORT_BASE="${AIRCD_HTTP_PORT_BASE:-7667}"
AGENT_SPECS="${AIRCD_AGENT_SPECS:-agent-a:agent-a-token:$CALLER_DIR,agent-b:agent-b-token:$CALLER_DIR}"

SERVER_PID=""
declare -a AGENT_PIDS=()

usage() {
    cat <<EOF
Usage: ./scripts/start-workspace.sh [options]

Starts:
  1. one local aircd server
  2. multiple airc-daemon agent wrappers
  3. leaves the shell attached so Ctrl-C shuts everything down cleanly

Options:
  --bind ADDR           server bind address (default: $BIND_ADDR)
  --db PATH             sqlite database path (default: $DB_PATH)
  --channels LIST       comma-separated shared channels (default: $CHANNELS)
  --model MODEL         Claude model for all agents (default: $MODEL)
  --permissions-mode M  Claude permissions mode: auto|skip (default: $PERMISSIONS_MODE)
  --http-port-base N    first daemon HTTP port (default: $HTTP_PORT_BASE)
  --agents SPECS        comma-separated specs: nick:token[:working_dir]
  -h, --help            show this help

Example:
  ./scripts/start-workspace.sh \\
    --channels '#work,#general' \\
    --agents 'agent-a:agent-a-token:/repo,agent-b:agent-b-token:/repo'

Human IRC client settings:
  host:     ${BIND_ADDR%:*}
  port:     ${BIND_ADDR##*:}
  password: human-token
  nick:     human
EOF
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --bind)
            BIND_ADDR="$2"
            shift 2
            ;;
        --db)
            DB_PATH="$2"
            shift 2
            ;;
        --channels)
            CHANNELS="$2"
            shift 2
            ;;
        --model)
            MODEL="$2"
            shift 2
            ;;
        --permissions-mode)
            PERMISSIONS_MODE="$2"
            shift 2
            ;;
        --http-port-base)
            HTTP_PORT_BASE="$2"
            shift 2
            ;;
        --agents)
            AGENT_SPECS="$2"
            shift 2
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            echo "Unknown argument: $1" >&2
            usage >&2
            exit 1
            ;;
    esac
done

mkdir -p "$LOG_DIR"

cleanup() {
    local pid
    echo ""
    echo "Stopping workspace..."
    for pid in "${AGENT_PIDS[@]}"; do
        if [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null; then
            kill "$pid" 2>/dev/null || true
            wait "$pid" 2>/dev/null || true
        fi
    done
    if [[ -n "$SERVER_PID" ]] && kill -0 "$SERVER_PID" 2>/dev/null; then
        kill "$SERVER_PID" 2>/dev/null || true
        wait "$SERVER_PID" 2>/dev/null || true
    fi
}
trap cleanup EXIT INT TERM

echo "Starting local aircd workspace..."
echo "  bind:        $BIND_ADDR"
echo "  db:          $DB_PATH"
echo "  channels:    $CHANNELS"
echo "  model:       $MODEL"
echo "  permissions: $PERMISSIONS_MODE"
echo ""

"$REPO_ROOT/scripts/start-server.sh" --db "$DB_PATH" --bind "$BIND_ADDR" \
    >"$LOG_DIR/server.log" 2>&1 &
SERVER_PID=$!
sleep 1

if ! kill -0 "$SERVER_PID" 2>/dev/null; then
    echo "Server failed to start. See $LOG_DIR/server.log" >&2
    exit 1
fi

if ! command -v claude >/dev/null 2>&1; then
    echo "claude CLI not found in PATH" >&2
    exit 1
fi

cd "$REPO_ROOT/clients/python"
if [[ ! -d .venv ]]; then
    python3 -m venv .venv
fi
.venv/bin/pip install -e ".[daemon]" --quiet
cd "$REPO_ROOT"

IFS=',' read -r -a AGENTS <<< "$AGENT_SPECS"
for index in "${!AGENTS[@]}"; do
    spec="${AGENTS[$index]}"
    IFS=':' read -r nick token working_dir <<< "$spec"
    if [[ -z "$nick" || -z "$token" ]]; then
        echo "Invalid agent spec: $spec" >&2
        exit 1
    fi
    if [[ -z "${working_dir:-}" ]]; then
        working_dir="$CALLER_DIR"
    fi
    http_port=$((HTTP_PORT_BASE + index))
    log_file="$LOG_DIR/${nick}.log"

    "$REPO_ROOT/scripts/start-agent.sh" \
        --host "${BIND_ADDR%:*}" \
        --port "${BIND_ADDR##*:}" \
        --token "$token" \
        --nick "$nick" \
        --channels "$CHANNELS" \
        --model "$MODEL" \
        --permissions-mode "$PERMISSIONS_MODE" \
        --working-dir "$working_dir" \
        --http-port "$http_port" \
        --skip-install \
        >"$log_file" 2>&1 &

    AGENT_PIDS+=("$!")
    echo "  agent $nick started -> $log_file"
done

echo ""
echo "Human IRC client settings"
echo "  host:     ${BIND_ADDR%:*}"
echo "  port:     ${BIND_ADDR##*:}"
echo "  password: human-token"
echo "  nick:     human"
echo "  channels: $CHANNELS"
echo ""
echo "Logs"
echo "  server:   $LOG_DIR/server.log"
for spec in "${AGENTS[@]}"; do
    IFS=':' read -r nick _rest <<< "$spec"
    echo "  $nick:     $LOG_DIR/${nick}.log"
done
echo ""
echo "Workspace is running. Press Ctrl-C to stop server and agents."

while true; do
    if ! kill -0 "$SERVER_PID" 2>/dev/null; then
        echo "Server exited unexpectedly. See $LOG_DIR/server.log" >&2
        exit 1
    fi
    for pid in "${AGENT_PIDS[@]}"; do
        if ! kill -0 "$pid" 2>/dev/null; then
            echo "An agent exited unexpectedly. Check logs in $LOG_DIR" >&2
            exit 1
        fi
    done
    sleep 2
done
