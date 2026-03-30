#!/bin/bash
set -e

INSTANCE="${1:-1}"
CHROME="$HOME/.cache/ms-playwright/chromium-1208/chrome-linux64/chrome"
EXTENSION="$HOME/flow2api/chrome-extension"
PROFILE_SRC="$HOME/flow2api/captcha_profile"
PROFILE_RUN="/tmp/captcha_profile_runtime_${INSTANCE}"
DEBUG_PORT=$((9224 + INSTANCE))
WS_URL="ws://localhost:8000/ws/captcha"

# Each instance uses a different upstream Oxylabs port for IP diversity
PROXY_USER="user-resproxy_EvTtK"
PROXY_PASS="bok~80__MFTCypbH"
PROXY_HOST="disp.oxylabs.io"
PORTS=(8002 8003 8004 8005)
PORT_INDEX=$(( (INSTANCE - 1) % ${#PORTS[@]} ))
UPSTREAM_PORT="${PORTS[$PORT_INDEX]}"
UPSTREAM_URL="http://${PROXY_USER}:${PROXY_PASS}@${PROXY_HOST}:${UPSTREAM_PORT}"

# Spin up a local proxy_bridge to handle auth
LOCAL_PROXY_PORT=$((13100 + INSTANCE))
$HOME/flow2api/venv/bin/python3 $HOME/flow2api/proxy_bridge.py "$UPSTREAM_URL" --port "$LOCAL_PROXY_PORT" &
PROXY_PID=$!

for i in $(seq 1 20); do
  if nc -z 127.0.0.1 "$LOCAL_PROXY_PORT" 2>/dev/null; then break; fi
  sleep 0.2
done

trap "kill $PROXY_PID 2>/dev/null" EXIT

# Inject wsUrl (idempotent)
sed -i "s|let wsUrl = '';|let wsUrl = '${WS_URL}';|" "$EXTENSION/background.js"

# Fresh copy of profile to /tmp
rm -rf "$PROFILE_RUN"
cp -a "$PROFILE_SRC" "$PROFILE_RUN"
rm -f "$PROFILE_RUN"/Singleton{Lock,Socket,Cookie}

echo "[captcha-browser-${INSTANCE}] proxy=localhost:${LOCAL_PROXY_PORT} -> ${PROXY_HOST}:${UPSTREAM_PORT}, debug=:${DEBUG_PORT}"

xvfb-run --auto-servernum --server-args="-screen 0 1280x720x24" \
  "$CHROME" \
    --no-sandbox --disable-dev-shm-usage --disable-gpu \
    --no-first-run --no-default-browser-check \
    --proxy-server=http://127.0.0.1:${LOCAL_PROXY_PORT} \
    --load-extension="$EXTENSION" \
    --disable-extensions-except="$EXTENSION" \
    --user-data-dir="$PROFILE_RUN" \
    --remote-debugging-port="$DEBUG_PORT" \
    "https://labs.google/fx"
