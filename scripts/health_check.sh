#!/usr/bin/env bash
# scripts/health_check.sh
# ─────────────────────────────────────────────────────────────────────────────
# Liveness probe used by Railway / Render / Docker healthcheck.
# Hits the /health endpoint and validates both Supabase and Redis are "ok".
#
# Exit 0  → healthy
# Exit 1  → unhealthy (platform will restart the container)
#
# Usage:
#   ./scripts/health_check.sh                  # uses localhost:8000
#   ./scripts/health_check.sh https://your.app # custom base URL
# ─────────────────────────────────────────────────────────────────────────────

set -euo pipefail

BASE_URL="${1:-http://localhost:8000}"
HEALTH_URL="${BASE_URL}/health"
TIMEOUT=10

# Fetch the health endpoint
response=$(curl --silent --max-time "$TIMEOUT" --fail "$HEALTH_URL" 2>/dev/null) || {
    echo "UNHEALTHY: /health endpoint unreachable at $HEALTH_URL"
    exit 1
}

# Parse the top-level status field
overall=$(echo "$response" | python3 -c "
import sys, json
try:
    data = json.load(sys.stdin)
    print(data.get('status', 'unknown'))
except Exception as e:
    print('parse_error')
" 2>/dev/null)

# Parse individual service checks
supabase=$(echo "$response" | python3 -c "
import sys, json
try:
    data = json.load(sys.stdin)
    print(data.get('checks', {}).get('supabase', 'unknown'))
except:
    print('unknown')
" 2>/dev/null)

redis_status=$(echo "$response" | python3 -c "
import sys, json
try:
    data = json.load(sys.stdin)
    print(data.get('checks', {}).get('redis', 'unknown'))
except:
    print('unknown')
" 2>/dev/null)

timestamp=$(echo "$response" | python3 -c "
import sys, json
try:
    data = json.load(sys.stdin)
    print(data.get('timestamp', '')[:19])
except:
    print('')
" 2>/dev/null)

echo "Health check at $timestamp"
echo "  Overall:   $overall"
echo "  Supabase:  $supabase"
echo "  Redis:     $redis_status"

if [ "$overall" = "ok" ]; then
    echo "HEALTHY"
    exit 0
else
    echo "UNHEALTHY: status=$overall supabase=$supabase redis=$redis_status"
    exit 1
fi
