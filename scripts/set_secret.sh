#!/usr/bin/env bash
# Usage: ./scripts/set_secret.sh NAME        (prompts for the value, hidden)
#        ./scripts/set_secret.sh NAME value  (for non-secret settings like CC_OWNER_EMAILS)
# Writes NAME=value into ~/.cc-secrets/env on the Sprite and restarts the agent.
set -euo pipefail
export PATH="$HOME/.local/bin:$PATH"
SPRITE="${SPRITE:-cc-main}"
NAME="$1"
if [ $# -ge 2 ]; then VALUE="$2"; else read -rsp "Value for $NAME (hidden): " VALUE; echo; fi
printf '%s=%s\n' "$NAME" "$VALUE" | sprite exec --stdin -s "$SPRITE" -- sh -c "
  f=/home/sprite/.cc-secrets/env; mkdir -p \$(dirname \$f); touch \$f; chmod 600 \$f
  grep -v '^$NAME=' \$f > \$f.tmp || true; cat >> \$f.tmp; mv \$f.tmp \$f; chmod 600 \$f
  mkdir -p /tmp/everett && date -u +%Y-%m-%dT%H:%M:%S+00:00 > /tmp/everett/deployed_at
  sprite-env services restart cc >/dev/null 2>&1 || true"
echo "✓ $NAME set on $SPRITE (agent restarted)"
