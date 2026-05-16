#!/usr/bin/env bash
# verify_deploy.sh — confirm a push has gone live on Render, then run
# read-only smoke checks against the deployed endpoints.
#
# Usage:
#   ./scripts/verify_deploy.sh            # uses HEAD as expected SHA
#   ./scripts/verify_deploy.sh <sha>      # uses provided SHA
#
# Environment:
#   ATLAS_LIVE_URL   default: https://tlg-amazon-intelligence-dashboard.onrender.com
#   POLL_TIMEOUT     default: 480 (seconds, ~8 min)
#   POLL_INTERVAL    default: 20  (seconds)
#
# Exit codes:
#   0  live SHA matches + smoke checks pass
#   1  timed out waiting for new SHA
#   2  smoke check failed (live SHA matched but an endpoint is broken)

set -euo pipefail

LIVE="${ATLAS_LIVE_URL:-https://tlg-amazon-intelligence-dashboard.onrender.com}"
TIMEOUT="${POLL_TIMEOUT:-480}"
INTERVAL="${POLL_INTERVAL:-20}"
EXPECTED_SHA="${1:-$(git rev-parse HEAD 2>/dev/null || echo "")}"
EXPECTED_SHORT="${EXPECTED_SHA:0:7}"

if [[ -z "${EXPECTED_SHA}" ]]; then
  echo "verify_deploy: no expected SHA (pass one, or run inside a git repo)"
  exit 2
fi

echo "verify_deploy: live=${LIVE}"
echo "verify_deploy: expecting SHA ${EXPECTED_SHORT} (full: ${EXPECTED_SHA})"
echo "verify_deploy: poll every ${INTERVAL}s, timeout ${TIMEOUT}s"

# ─── Phase 1: poll /api/version until SHA matches ─────────────────────────
deadline=$((SECONDS + TIMEOUT))
last_live_sha="?"
while (( SECONDS < deadline )); do
  body=$(curl -s --max-time 8 "${LIVE}/api/version" 2>/dev/null || echo "")
  live_sha=$(echo "${body}" | python3 -c 'import json,sys; d=json.loads(sys.stdin.read() or "{}"); print(d.get("sha") or "")' 2>/dev/null || echo "")
  if [[ -n "${live_sha}" && "${live_sha}" != "${last_live_sha}" ]]; then
    echo "  [$(date +%H:%M:%S)] live SHA = ${live_sha:0:7}"
    last_live_sha="${live_sha}"
  fi
  if [[ "${live_sha}" == "${EXPECTED_SHA}" ]]; then
    echo "verify_deploy: ✓ live matches expected SHA"
    break
  fi
  sleep "${INTERVAL}"
done

# Confirm we actually broke out of the loop (vs. ran out of time)
if [[ "${last_live_sha}" != "${EXPECTED_SHA}" ]]; then
  echo "verify_deploy: ✗ timed out after ${TIMEOUT}s — live still on ${last_live_sha:0:7}"
  echo "                Check Render dashboard for build/deploy errors."
  exit 1
fi

# ─── Phase 2: read-only smoke checks ──────────────────────────────────────
echo "verify_deploy: running smoke checks…"
fail=0

smoke() {
  local label="$1" path="$2" expect="$3"
  local out
  out=$(curl -s -w "\n__HTTP__%{http_code}" --max-time 12 "${LIVE}${path}" 2>/dev/null || echo "__HTTP__000")
  local code="${out##*__HTTP__}"
  local body="${out%__HTTP__*}"
  if [[ "${code}" != "200" ]]; then
    echo "  [FAIL] ${label} → HTTP ${code}"
    fail=$((fail + 1))
    return
  fi
  if [[ -n "${expect}" && ! "${body}" =~ ${expect} ]]; then
    echo "  [FAIL] ${label} → 200 but body missing /${expect}/"
    echo "          body[0..200]: ${body:0:200}"
    fail=$((fail + 1))
    return
  fi
  echo "  [PASS] ${label}"
}

smoke "version"               "/api/version"                          '"ok":true'
smoke "visible-brands"        "/api/atlas/visible-brands"             '"ok":true'
smoke "operator"              "/api/atlas/operator"                   '"ok":true'
smoke "operators"             "/api/atlas/operators"                  '"ok":true'
smoke "inputs/freshness"      "/api/atlas/inputs/freshness"           '"ok":true'
smoke "inputs/history"        "/api/atlas/inputs/history?limit=1"     '"ok":true'
smoke "memory/sessions"       "/api/atlas/memory/sessions?limit=1"    '"ok":true'

if (( fail > 0 )); then
  echo "verify_deploy: ✗ ${fail} smoke check(s) failed"
  exit 2
fi
echo "verify_deploy: ✓ all smoke checks passed at SHA ${EXPECTED_SHORT}"
