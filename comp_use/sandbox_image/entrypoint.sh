#!/usr/bin/env bash
set -euo pipefail

export DISPLAY="${DISPLAY:-:99}"
export SCREEN_WIDTH="${SCREEN_WIDTH:-1366}"
export SCREEN_HEIGHT="${SCREEN_HEIGHT:-768}"
export SCREEN_DEPTH="${SCREEN_DEPTH:-24}"

# Neutral defaults: no hardcoded site/settings beyond a standard PC screen.
export CHROME_URL="${CHROME_URL:-about:blank}"
export BROWSER="${BROWSER:-chromium}"
export CHROME_CDP_PORT="${CHROME_CDP_PORT:-9222}"

exec /usr/bin/supervisord -c "/home/pwuser/sandbox/supervisord.conf"

