#!/usr/bin/env bash
#
# Start a local aircd server with practical defaults for human + agent testing.
#
# Usage:
#   ./scripts/start-server.sh
#   ./scripts/start-server.sh --db /tmp/aircd.sqlite3 --bind 127.0.0.1:6667

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
STATE_DIR_DEFAULT="$REPO_ROOT/.aircd"

DB_PATH="${AIRCD_DB:-$STATE_DIR_DEFAULT/local.sqlite3}"
BIND_ADDR="${AIRCD_BIND:-127.0.0.1:6667}"
DO_BUILD=1

usage() {
    cat <<EOF
Usage: ./scripts/start-server.sh [options]

Starts the aircd server in the foreground.

Options:
  --db PATH        SQLite database path (default: $DB_PATH)
  --bind ADDR      Bind address host:port (default: $BIND_ADDR)
  --no-build       Skip cargo build and use the existing release binary
  -h, --help       Show this help

Environment:
  AIRCD_DB         Default database path
  AIRCD_BIND       Default bind address
EOF
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --db)
            DB_PATH="$2"
            shift 2
            ;;
        --bind)
            BIND_ADDR="$2"
            shift 2
            ;;
        --no-build)
            DO_BUILD=0
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

mkdir -p "$(dirname "$DB_PATH")"

cd "$REPO_ROOT"
if [[ "$DO_BUILD" -eq 1 ]]; then
    echo "Building aircd release binary..."
    cargo build --release --quiet
fi

echo "Starting aircd"
echo "  bind: $BIND_ADDR"
echo "  db:   $DB_PATH"

exec env AIRCD_DB="$DB_PATH" AIRCD_BIND="$BIND_ADDR" ./target/release/aircd
