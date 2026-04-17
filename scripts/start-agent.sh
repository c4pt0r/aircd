#!/usr/bin/env bash
#
# Start one wrapper-backed agent against a local or remote aircd instance.
#
# Usage:
#   ./scripts/start-agent.sh
#   ./scripts/start-agent.sh --nick agent-b --token agent-b-token --channels '#work,#general'

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
CALLER_DIR="$(pwd)"

HOST="${AIRCD_HOST:-localhost}"
PORT="${AIRCD_PORT:-6667}"
TOKEN="${AIRCD_TOKEN:-agent-a-token}"
NICK="${AIRCD_NICK:-agent-a}"
CHANNELS="${AIRCD_CHANNELS:-#work}"
MODEL="${CLAUDE_MODEL:-sonnet}"
PERMISSIONS_MODE="${AIRCD_PERMISSIONS_MODE:-skip}"
WORKING_DIR="${AIRCD_WORKING_DIR:-$CALLER_DIR}"
HTTP_PORT="${AIRCD_HTTP_PORT:-7667}"
TLS=0
TLS_INSECURE=0
TLS_CA="${AIRCD_TLS_CA:-}"
VERBOSE=0
SKIP_INSTALL=0

usage() {
    cat <<EOF
Usage: ./scripts/start-agent.sh [options]

Starts one airc-daemon process in the foreground.
This helper defaults to --permissions-mode skip because it is intended for
headless local wrapper sessions.

Options:
  --host HOST             aircd host (default: $HOST)
  --port PORT             aircd port (default: $PORT)
  --token TOKEN           agent token (default: $TOKEN)
  --nick NICK             agent nick (default: $NICK)
  --channels LIST         comma-separated channels (default: $CHANNELS)
  --model MODEL           Claude model (default: $MODEL)
  --permissions-mode MODE Claude permissions mode: auto|skip (default: $PERMISSIONS_MODE)
  --working-dir PATH      Claude working directory (default: $WORKING_DIR)
  --http-port PORT        local daemon HTTP port (default: $HTTP_PORT)
  --tls                   connect with TLS
  --tls-insecure          disable TLS certificate verification
  --tls-ca PATH           CA certificate path
  --skip-install          reuse existing clients/python/.venv without reinstalling
  -v, --verbose           enable verbose daemon logging
  -h, --help              show this help

Defaults target the seeded local principals:
  human / human-token
  agent-a / agent-a-token
  agent-b / agent-b-token
  agent-c / agent-c-token
EOF
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --host)
            HOST="$2"
            shift 2
            ;;
        --port)
            PORT="$2"
            shift 2
            ;;
        --token)
            TOKEN="$2"
            shift 2
            ;;
        --nick)
            NICK="$2"
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
        --working-dir)
            WORKING_DIR="$2"
            shift 2
            ;;
        --http-port)
            HTTP_PORT="$2"
            shift 2
            ;;
        --tls)
            TLS=1
            shift
            ;;
        --tls-insecure)
            TLS_INSECURE=1
            shift
            ;;
        --tls-ca)
            TLS_CA="$2"
            shift 2
            ;;
        --skip-install)
            SKIP_INSTALL=1
            shift
            ;;
        -v|--verbose)
            VERBOSE=1
            shift
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

if [[ ! -d "$WORKING_DIR" ]]; then
    echo "Working directory does not exist or is not a directory: $WORKING_DIR" >&2
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
if [[ "$SKIP_INSTALL" -eq 0 ]]; then
    .venv/bin/pip install -e ".[daemon]" --quiet
fi

ARGS=(
    --host "$HOST"
    --port "$PORT"
    --token "$TOKEN"
    --nick "$NICK"
    --channels "$CHANNELS"
    --model "$MODEL"
    --permissions-mode "$PERMISSIONS_MODE"
    --working-dir "$WORKING_DIR"
    --http-port "$HTTP_PORT"
)

if [[ "$TLS" -eq 1 ]]; then
    ARGS+=(--tls)
fi
if [[ "$TLS_INSECURE" -eq 1 ]]; then
    ARGS+=(--tls-insecure)
fi
if [[ -n "$TLS_CA" ]]; then
    ARGS+=(--tls-ca "$TLS_CA")
fi
if [[ "$VERBOSE" -eq 1 ]]; then
    ARGS+=(--verbose)
fi

echo "Starting wrapper-backed agent"
echo "  nick:         $NICK"
echo "  token:        $TOKEN"
echo "  channels:     $CHANNELS"
echo "  working dir:  $WORKING_DIR"
echo "  daemon http:  $HTTP_PORT"

exec .venv/bin/airc-daemon "${ARGS[@]}"
