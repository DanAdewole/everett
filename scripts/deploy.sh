#!/usr/bin/env bash
# Ship ./agent to the cc-main Sprite, install deps, and (re)start the web service on :8080.
set -euo pipefail
cd "$(dirname "$0")/.."
export PATH="$HOME/.local/bin:$PATH"
SPRITE="${SPRITE:-cc-main}"

echo "→ uploading code"
COPYFILE_DISABLE=1 tar czf - --no-xattrs --no-mac-metadata --exclude '__pycache__' --exclude '.venv' -C agent . \
  | sprite exec --stdin -s "$SPRITE" -- sh -c 'mkdir -p /home/sprite/app && tar xzf - -C /home/sprite/app'

echo "→ installing deps + secrets bootstrap"
sprite exec --no-stdin -s "$SPRITE" -- sh -c '
set -e
cd /home/sprite/app
[ -d .venv ] || uv venv -q .venv
uv pip install -q -p .venv/bin/python -r requirements.txt
mkdir -p /home/sprite/.cc-secrets && chmod 700 /home/sprite/.cc-secrets
touch /home/sprite/.cc-secrets/env && chmod 600 /home/sprite/.cc-secrets/env
grep -q "^CC_DASH_KEY=" /home/sprite/.cc-secrets/env || echo "CC_DASH_KEY=$(python3 -c "import secrets;print(secrets.token_urlsafe(12))")" >> /home/sprite/.cc-secrets/env
.venv/bin/python -c "import app.main" && echo "import ok"
'

echo "→ (re)starting service"
sprite exec --no-stdin -s "$SPRITE" -- sh -c '
# Undo must never restore a checkpoint taken before this deploy (that would roll back the code).
mkdir -p /tmp/everett && chmod 700 /tmp/everett && date -u +%Y-%m-%dT%H:%M:%S+00:00 > /tmp/everett/deployed_at
if sprite-env services get cc >/dev/null 2>&1; then
  sprite-env services restart cc
else
  sprite-env services create cc --cmd /home/sprite/app/.venv/bin/uvicorn \
    --args app.main:app,--host,0.0.0.0,--port,8080 --dir /home/sprite/app --http-port 8080 --no-stream
fi'
echo "✓ deployed"
