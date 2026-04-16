#!/usr/bin/env bash
#
# Start a human-side irssi session against aircd using the seeded local
# principal. This uses an ephemeral irssi home so the startup commands do not
# modify the user's existing irssi configuration.
#
# Usage:
#   ./scripts/start-human-irssi.sh
#   ./scripts/start-human-irssi.sh --host 127.0.0.1 --port 6667 --channels '#work,#general'

set -euo pipefail

HOST="${AIRCD_HOST:-127.0.0.1}"
PORT="${AIRCD_PORT:-6667}"
PASSWORD="${AIRCD_PASSWORD:-human-token}"
NICK="${AIRCD_NICK:-human}"
CHANNELS="${AIRCD_CHANNELS:-#work}"
IRSSI_BIN="${IRSSI_BIN:-irssi}"
TEMP_HOME=""

usage() {
    cat <<EOF
Usage: ./scripts/start-human-irssi.sh [options]

Launches irssi for the seeded human principal and auto-connects to aircd.
The script creates a temporary HOME and ~/.irssi/startup file so your normal
irssi configuration is left untouched.

Options:
  --host HOST        server host (default: $HOST)
  --port PORT        server port (default: $PORT)
  --password PASS    IRC server password / PASS token (default: $PASSWORD)
  --nick NICK        IRC nick (default: $NICK)
  --channels LIST    comma-separated channel list (default: $CHANNELS)
  --irssi-bin PATH   irssi binary to execute (default: $IRSSI_BIN)
  -h, --help         show this help

Example:
  ./scripts/start-human-irssi.sh --channels '#work,#general'
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
        --password)
            PASSWORD="$2"
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
        --irssi-bin)
            IRSSI_BIN="$2"
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

if ! command -v "$IRSSI_BIN" >/dev/null 2>&1; then
    echo "irssi not found: $IRSSI_BIN" >&2
    exit 1
fi

TEMP_HOME="$(mktemp -d "${TMPDIR:-/tmp}/aircd-irssi-XXXXXX")"
IRSSI_HOME="$TEMP_HOME/.irssi"
mkdir -p "$IRSSI_HOME"

cleanup() {
    if [[ -n "$TEMP_HOME" && -d "$TEMP_HOME" ]]; then
        rm -rf "$TEMP_HOME"
    fi
}
trap cleanup EXIT

cat >"$IRSSI_HOME/startup" <<EOF
/ECHO Using temporary irssi home: $IRSSI_HOME
/CONNECT $HOST $PORT $PASSWORD $NICK
/WAIT 1000
/JOIN $CHANNELS
/ECHO Connected to aircd as $NICK on $HOST:$PORT
EOF

echo "Launching irssi"
echo "  host:     $HOST"
echo "  port:     $PORT"
echo "  password: $PASSWORD"
echo "  nick:     $NICK"
echo "  channels: $CHANNELS"
echo ""
echo "The session uses a temporary irssi home and cleans it up on exit."

HOME="$TEMP_HOME" "$IRSSI_BIN"
