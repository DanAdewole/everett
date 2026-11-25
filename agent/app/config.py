"""Runtime configuration. Secrets live in ~/.cc-secrets/env on the Sprite (never in the repo)."""
import os
from pathlib import Path

HOME = Path(os.environ.get("HOME", "/home/sprite"))
SECRETS_FILE = HOME / ".cc-secrets" / "env"
# The ledger lives on /tmp: a separate disk that checkpoint restores do NOT rewind (verified). It is Everett's
# flight recorder: it survives time travel, so the timeline still shows what was undone and why.
DATA_DIR = Path(os.environ.get("CC_DATA_DIR", "/tmp/everett"))
DATA_DIR.mkdir(parents=True, exist_ok=True)
os.chmod(DATA_DIR, 0o700)
DB_PATH = DATA_DIR / "cc.db"
_OLD_DB = HOME / "app-data" / "cc.db"
if not DB_PATH.exists() and _OLD_DB.exists():
    import sqlite3
    with sqlite3.connect(_OLD_DB) as _src, sqlite3.connect(DB_PATH) as _dst:
        _src.backup(_dst)  # WAL-safe, unlike a file copy
# Written by deploy.sh / set_secret.sh. Restoring a checkpoint older than this would roll back code or secrets.
DEPLOY_MARK = DATA_DIR / "deployed_at"
# Everett's local vault: lives on the checkpointed disk, so time travel visibly rewinds it.
VAULT_DIR = HOME / "vault"


def _load_secrets() -> None:
    if not SECRETS_FILE.exists():
        return
    for line in SECRETS_FILE.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())


_load_secrets()

AGENT_NAME = os.environ.get("CC_AGENT_NAME", "Everett")
INBOX = os.environ.get("CC_INBOX", "cc.everett@agentmail.to")
PUBLIC_URL = os.environ.get("CC_PUBLIC_URL", "https://cc-main-hezx.sprites.app")
TIMEZONE = os.environ.get("CC_TIMEZONE", "America/New_York")
# Comma-separated addresses allowed to command the agent. Empty = anyone (demo mode).
OWNER_EMAILS = {e.strip().lower() for e in os.environ.get("CC_OWNER_EMAILS", "").split(",") if e.strip()}
# Shared secret that guards the dashboard and the undo buttons.
DASH_KEY = os.environ.get("CC_DASH_KEY", "")
# Svix secret from AgentMail webhook creation (whsec_...). Empty = skip verification.
WEBHOOK_SECRET = os.environ.get("CC_WEBHOOK_SECRET", "")
# Sprites API token (org token) for Parallel Lives.
SPRITES_TOKEN = os.environ.get("SPRITES_TOKEN", "")
SPRITES_API = os.environ.get("SPRITES_API_URL", "https://api.sprites.dev")

BOX_ROOT_FOLDER = os.environ.get("CC_BOX_ROOT", "Everett Files")
PINECONE_INDEX = os.environ.get("CC_PINECONE_INDEX", "cc-memory")
PINECONE_NS = os.environ.get("CC_PINECONE_NS", "memories")

GEMINI_MODELS = [m.strip() for m in os.environ.get(
    "CC_GEMINI_MODELS",
    "gemini-3.5-flash,gemini-3-flash-preview,gemini-3.8-flash,gemini-3.7-flash,gemini-3.6-flash,gemini-flash-latest,gemini-3.5-flash-lite,gemini-flash-lite-latest,gemini-3.1-flash-lite",
).split(",") if m.strip()]

MAX_LIVES = 2  # Free plan caps an org at 3 Sprites: cc-main + 2 timelines.
# Anyone may email the agent (judges included); non-owners get a private "guest" space with daily caps
# so nobody can burn the free credits.
GUEST_DAILY_RUNS = int(os.environ.get("CC_GUEST_DAILY_RUNS", "15"))
GUEST_DAILY_LIVES = int(os.environ.get("CC_GUEST_DAILY_LIVES", "2"))


def tenant_of(email: str) -> str:
    """Owner mail shares one private space; every other sender gets their own."""
    import hashlib
    if email in OWNER_EMAILS:
        return "owner"
    return "g-" + hashlib.sha256(email.encode()).hexdigest()[:12]


def _sig(msg: str) -> str:
    import hashlib
    import hmac
    return hmac.new(DASH_KEY.encode(), msg.encode(), hashlib.sha256).hexdigest()[:32]


def make_token(tenant: str, kind: str, ttl_days: float, start: float | None = None) -> str:
    """'<tenant>.<expiry>.<hmac>': scoped to one tenant, one purpose (login link vs browser session), and expiring.
    The master key never leaves the Sprite."""
    import time
    exp = int((start or time.time()) + ttl_days * 86400)
    return f"{tenant}.{exp}.{_sig(f'{kind}|{tenant}|{exp}')}"


def verify_token(token: str, kind: str) -> str | None:
    """Returns the tenant if the token is genuine and unexpired."""
    import hmac
    import time
    try:
        tenant, exp, sig = (token or "").rsplit(".", 2)
        if int(exp) < time.time():
            return None
    except ValueError:
        return None
    return tenant if hmac.compare_digest(sig, _sig(f"{kind}|{tenant}|{exp}")) else None


def home_path(tenant: str) -> str:
    return "/" if tenant == "owner" else f"/u/{tenant}"


def tenant_link(tenant: str, path: str = "", valid_from: float | None = None) -> str:
    """Link for emails: a signed, expiring login token that only opens this tenant's own space.
    It's exchanged for an HttpOnly cookie on first click, so nothing sensitive stays in the address bar."""
    from urllib.parse import quote
    nxt = (home_path(tenant).rstrip("/") + path) or "/"
    return f"{PUBLIC_URL}/go?t={make_token(tenant, 'login', 14, valid_from)}&next={quote(nxt, safe='/')}"
