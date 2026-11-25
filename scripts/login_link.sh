#!/usr/bin/env bash
# Print a fresh owner sign-in link for the dashboard (valid 14 days, single tenant, swapped for a cookie on click).
# Usage: ./scripts/login_link.sh [tenant]     (default: owner)
set -euo pipefail
export PATH="$HOME/.local/bin:$PATH"
SPRITE="${SPRITE:-cc-main}"
sprite exec --no-stdin -s "$SPRITE" -- sh -c "cd /home/sprite/app && .venv/bin/python -c \"from app import config; print(config.tenant_link('${1:-owner}'))\""
