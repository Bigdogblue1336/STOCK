#!/usr/bin/env bash
# Runs the full portfolio monitor pipeline (portfolio.cli: data pull, valuation,
# portfolio analytics, macro gate, news analysis, diff/alerts, notifier) once and
# appends output to a dated log file. Intended to be invoked by cron once each
# weekday after market close -- see crontab.example in this directory.
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(dirname "$SCRIPT_DIR")"
cd "$REPO_ROOT"

# cron runs with a near-empty environment. Pick up API keys / notifier config
# from a local .env if present (create one from .env.example; never commit it).
if [ -f "$REPO_ROOT/.env" ]; then
    set -a
    # shellcheck disable=SC1091
    source "$REPO_ROOT/.env"
    set +a
fi

# Prefer a project virtualenv if one exists.
if [ -f "$REPO_ROOT/venv/bin/activate" ]; then
    # shellcheck disable=SC1091
    source "$REPO_ROOT/venv/bin/activate"
elif [ -f "$REPO_ROOT/.venv/bin/activate" ]; then
    # shellcheck disable=SC1091
    source "$REPO_ROOT/.venv/bin/activate"
fi

LOG_DIR="$REPO_ROOT/logs"
mkdir -p "$LOG_DIR"
LOG_FILE="$LOG_DIR/portfolio_$(date +%Y-%m-%d).log"

PYTHON_BIN="${PYTHON_BIN:-python3}"

{
    echo "=== Run started $(date -Iseconds) ==="
    "$PYTHON_BIN" -m portfolio.cli "$@"
    status=$?
    echo "=== Run finished $(date -Iseconds), exit $status ==="
    exit $status
} >> "$LOG_FILE" 2>&1
