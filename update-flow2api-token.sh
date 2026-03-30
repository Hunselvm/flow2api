#!/bin/bash
# Refreshes Google session tokens by launching Playwright Chrome for each account,
# then pushes tokens to flow2api via its API (no restart needed).
#
# Uses separate Chrome profiles (refresh_user_data_dir) so flow2api can keep
# running while tokens are refreshed. flow2api is NOT stopped or restarted.
#
# Run manually or via cron (e.g. every 6 hours).
# Cron example:
#   0 */6 * * * /usr/local/bin/update-token >> /var/log/flow2api-token-update.log 2>&1

set -euo pipefail

FLOW2API_URL="${FLOW2API_URL:-http://localhost:8000}"
CONNECTION_TOKEN="${CONNECTION_TOKEN:-}"
SCRIPT_DIR="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"
ACCOUNTS_FILE="$HOME/.flow2api-accounts.json"
REFRESH_TIMEOUT="${REFRESH_TIMEOUT:-30}"
PYTHON="${FLOW2API_PYTHON:-python3}"

log() { echo "[$(date -Iseconds)] $*"; }

# Source env file if CONNECTION_TOKEN not set
if [ -z "$CONNECTION_TOKEN" ] && [ -f /etc/flow2api.env ]; then
  export $(grep -v '^#' /etc/flow2api.env | xargs)
  CONNECTION_TOKEN="${CONNECTION_TOKEN:-}"
fi

if [ -z "$CONNECTION_TOKEN" ]; then
  log "ERROR: CONNECTION_TOKEN not set"
  exit 1
fi

# Verify flow2api is running (we push tokens while it's up)
if ! curl -s --max-time 5 "${FLOW2API_URL}/login" > /dev/null 2>&1; then
  log "WARN: flow2api not reachable at ${FLOW2API_URL} - starting it"
  sudo systemctl start flow2api 2>/dev/null || true
  sleep 5
fi

# Build list of accounts with refresh-specific profiles
FLOW2API_DIR="${FLOW2API_DIR:-${SCRIPT_DIR}/flow2api}"
BROWSER_DATA="${FLOW2API_DIR}/browser_data"
REFRESH_PROFILES="${FLOW2API_DIR}/refresh_profiles"

declare -a LABELS=()
declare -a REFRESH_DIRS=()

if [ -f "$ACCOUNTS_FILE" ] && [ "$(jq length "$ACCOUNTS_FILE")" -gt 0 ]; then
  # Read refresh_user_data_dir (preferred) or fall back to user_data_dir
  while IFS=$'\t' read -r label refresh_dir fallback_dir; do
    dir="${refresh_dir}"
    [ "$dir" = "null" ] && dir="${fallback_dir}"
    # Expand ~ to $HOME
    dir="${dir/#\~/$HOME}"
    LABELS+=("$label")
    REFRESH_DIRS+=("$dir")
  done < <(jq -r '.[] | [(.label // "unknown"), (.refresh_user_data_dir // "null"), .user_data_dir] | @tsv' "$ACCOUNTS_FILE")
else
  # Fallback: scan browser_data for browser_* dirs, use refresh_profiles equivalents
  for d in "${BROWSER_DATA}"/browser_*/; do
    [ -d "$d" ] || continue
    label=$(basename "$d")
    LABELS+=("$label")
    # Prefer refresh profile if it exists, otherwise use original (requires flow2api stop)
    if [ -d "${REFRESH_PROFILES}/${label}" ]; then
      REFRESH_DIRS+=("${REFRESH_PROFILES}/${label}")
    else
      log "WARN: No refresh profile for ${label} - using original (may conflict with flow2api)"
      REFRESH_DIRS+=("${d%/}")
    fi
  done
fi

if [ ${#LABELS[@]} -eq 0 ]; then
  log "ERROR: No account directories found"
  exit 1
fi

log "Found ${#LABELS[@]} account(s)"

# --- Refresh tokens via Playwright (flow2api stays running) ---
declare -A TOKENS=()
ERRORS=0

for i in "${!LABELS[@]}"; do
  LABEL="${LABELS[$i]}"
  REFRESH_DIR="${REFRESH_DIRS[$i]}"
  log "Refreshing token for ${LABEL} (profile: ${REFRESH_DIR})..."

  TOKEN=""
  if OUTPUT=$("$PYTHON" "$SCRIPT_DIR/refresh-token.py" \
      --user-data-dir "$REFRESH_DIR" \
      --timeout "$REFRESH_TIMEOUT" 2>/dev/stderr); then
    TOKEN=$(echo "$OUTPUT" | tail -1)
  fi

  # Validate: non-empty, no spaces, only ASCII printable, >20 chars
  if [ -n "$TOKEN" ] && [[ "$TOKEN" != *" "* ]] && [ ${#TOKEN} -gt 20 ] && [[ "$TOKEN" =~ ^[[:print:]]+$ ]] && ! [[ "$TOKEN" =~ [^[:ascii:]] ]]; then
    TOKENS["$LABEL"]="$TOKEN"
    log "OK: Got token for ${LABEL} (${#TOKEN} chars)"
  else
    log "WARN: Failed to get token for ${LABEL}"
    ERRORS=$((ERRORS + 1))
  fi
done

# --- Push tokens to flow2api (already running) ---
PUSHED=0

for LABEL in "${!TOKENS[@]}"; do
  TOKEN="${TOKENS[$LABEL]}"

  RESPONSE=$(curl -s -w "\n%{http_code}" -X POST "${FLOW2API_URL}/api/plugin/update-token" \
    -H "Content-Type: application/json" \
    -H "Authorization: Bearer ${CONNECTION_TOKEN}" \
    -d "{\"session_token\": \"${TOKEN}\"}")

  HTTP_CODE=$(echo "$RESPONSE" | tail -1)
  BODY=$(echo "$RESPONSE" | sed '$d')

  if [ "$HTTP_CODE" -ge 200 ] && [ "$HTTP_CODE" -lt 300 ]; then
    log "PUSHED (${LABEL}): ${BODY}"
    PUSHED=$((PUSHED + 1))
  else
    log "ERROR pushing (${LABEL}): HTTP ${HTTP_CODE} - ${BODY}"
    ERRORS=$((ERRORS + 1))
  fi
done

log "Done: ${PUSHED} token(s) pushed, ${ERRORS} error(s)"

if [ "$ERRORS" -gt 0 ] && [ "$PUSHED" -eq 0 ]; then
  exit 1
fi
