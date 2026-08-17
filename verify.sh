#!/usr/bin/env bash
# Usage:  bash verify.sh https://your-app.onrender.com
# Tells you which of three stages the frontend changes stopped at.
DOMAIN="${1:-}"

echo "=== 1. IN YOUR FILES? (each number should be >= 1) ==="
echo -n "  Builds.tsx uses ShapeAllocation:  "
grep -c "ShapeAllocation" frontend/src/pages/Builds.tsx 2>/dev/null || echo "0 - file missing"
echo -n "  SetDetail.tsx has team exposure:  "
grep -c "team_exposures" frontend/src/pages/SetDetail.tsx 2>/dev/null || echo 0
echo -n "  SetDetail.tsx has shared domain:  "
grep -c "distDomain" frontend/src/pages/SetDetail.tsx 2>/dev/null || echo 0
echo -n "  Builds.tsx has position limits:   "
grep -c "posLimits" frontend/src/pages/Builds.tsx 2>/dev/null || echo 0
echo -n "  ShapeAllocation.tsx exists:       "
[ -f frontend/src/components/ShapeAllocation.tsx ] && echo yes || echo "NO - patch never applied"

echo
echo "=== 2. COMMITTED AND PUSHED? ==="
echo "-- uncommitted changes (empty is good):"
git status --short
echo "-- last 5 commits:"
git log --oneline -5
echo "-- commits NOT yet pushed (empty is good):"
git log origin/main..HEAD --oneline

if [ -z "$DOMAIN" ]; then
  echo
  echo "=== 3. SKIPPED - pass your domain: bash verify.sh https://your-app.onrender.com"
  exit 0
fi

echo
echo "=== 3. WHAT IS RENDER ACTUALLY SERVING? ==="
BUNDLE=$(curl -s "$DOMAIN/" | grep -o '/assets/index-[A-Za-z0-9_-]*\.js' | head -1)
echo "  served bundle: ${BUNDLE:-NONE FOUND}"
if [ -n "$BUNDLE" ]; then
  N=$(curl -s "$DOMAIN$BUNDLE" | grep -c "Stack shape allocation")
  echo "  'Stack shape allocation' occurrences in it: $N"
  [ "$N" -gt 0 ] && echo "  -> NEW code is live. If the UI still looks old, hard-refresh (Ctrl+Shift+R)." \
                 || echo "  -> Render is serving the OLD bundle. Check stage 2, then redeploy."
fi
