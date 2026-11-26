"""HTTP surface of the Sprite: AgentMail webhook, timeline dashboard, undo buttons, reminder tick."""
import asyncio
import base64
import hashlib
import hmac
import json
import logging
import threading
import time

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from . import config, db
from .agent import RUN_LOCK, handle_event, undo_run
from .dashboard import life_page, timeline_page
from .landing import landing_page
from .reminders import send_due
from .sprite import awake_begin, checkpoint_restore, keep_awake

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
log = logging.getLogger("cc.main")
app = FastAPI(title="Everett")


def _verify_svix(headers, body: bytes) -> bool:
    if not config.WEBHOOK_SECRET:
        return True
    msg_id, ts, sigs = headers.get("svix-id"), headers.get("svix-timestamp"), headers.get("svix-signature", "")
    if not (msg_id and ts) or abs(time.time() - int(ts)) > 600:
        return False
    secret = base64.b64decode(config.WEBHOOK_SECRET.split("_", 1)[-1])
    expected = base64.b64encode(hmac.new(secret, f"{msg_id}.{ts}.".encode() + body, hashlib.sha256).digest()).decode()
    return any(hmac.compare_digest(expected, s.split(",", 1)[-1]) for s in sigs.split())


SESSION = "everett_session"
SECURITY_HEADERS = {
    "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; "
                               "img-src 'self' data:; form-action 'self'; frame-ancestors 'none'; base-uri 'none'",
    "X-Frame-Options": "DENY",                  # no clickjacking the Undo buttons
    "Referrer-Policy": "no-referrer",           # URLs never leak to other sites
    "X-Content-Type-Options": "nosniff",
    "Cache-Control": "no-store",                # timelines hold private data: never cache
    "Strict-Transport-Security": "max-age=31536000",
}


@app.middleware("http")
async def security_headers(request: Request, call_next):
    resp = await call_next(request)
    for k, v in SECURITY_HEADERS.items():
        resp.headers.setdefault(k, v)
    return resp


def _session(request: Request) -> str | None:
    return config.verify_token(request.cookies.get(SESSION, ""), "session")


def _signed_in(resp, tenant: str):
    resp.set_cookie(SESSION, config.make_token(tenant, "session", 30), max_age=30 * 86400,
                    httponly=True, secure=True, samesite="lax", path="/")  # lax: cross-site POSTs carry no cookie
    return resp


def _denied() -> HTMLResponse:
    return HTMLResponse(f"<!doctype html><meta name=viewport content='width=device-width'><title>{config.AGENT_NAME}"
                        f"</title><body style='font:16px system-ui;max-width:560px;margin:60px auto;padding:0 16px'>"
                        f"<h1>This link has expired or isn't yours</h1><p>Email <b>{config.INBOX}</b> anything and "
                        f"my reply will include a fresh link to your timeline.</p>", status_code=403)


def _owner(request: Request) -> bool:
    return _session(request) == "owner"


def _can_view(request: Request, tenant: str) -> bool:
    return _session(request) in (tenant, "owner")


@app.get("/go")
def go(t: str = "", next: str = "/"):
    """Exchange a signed, expiring email link for an HttpOnly session cookie, then drop the token from the URL."""
    tenant = config.verify_token(t, "login")
    if not tenant:
        return _denied()
    home = config.home_path(tenant)
    # Only redirect inside this tenant's own space (no open redirects, no hopping into someone else's).
    dest = next if next.startswith(home) and "//" not in next and "\\" not in next else home
    return _signed_in(RedirectResponse(dest, status_code=303), tenant)


def _work(event: dict, task: str) -> None:
    with keep_awake(20, name=task):
        try:
            handle_event(event)
        except Exception:
            log.exception("event failed")


@app.post("/webhooks/agentmail")
async def agentmail_webhook(request: Request):
    body = await request.body()
    if not _verify_svix(request.headers, body):
        raise HTTPException(401, "bad signature")
    event = json.loads(body)
    if event.get("event_type") != "message.received":
        return {"ok": True, "ignored": event.get("event_type")}
    if not db.claim(f"evt:{event.get('event_id')}"):
        return {"ok": True, "duplicate": True}
    # Take the keep-awake task BEFORE acking: a Sprite that was just woken by this request could otherwise fall
    # back asleep in the gap between our 200 and the background thread starting (seen: a 37 s cold-start stall).
    task = await asyncio.to_thread(awake_begin, 20)
    threading.Thread(target=_work, args=(event, task), daemon=True).start()
    return {"ok": True}


@app.api_route("/tick", methods=["GET", "POST"])
def tick():
    """External cron pings this so reminders fire even while no email arrives."""
    return {"ok": True, "reminders_sent": send_due()}


@app.get("/health")
def health():
    return {"ok": True, "agent": config.AGENT_NAME}


@app.get("/about", response_class=HTMLResponse)
def about():
    """The public landing page, even for a signed-in owner."""
    return HTMLResponse(landing_page())


@app.get("/", response_class=HTMLResponse)
def home(request: Request):
    k = request.query_params.get("k", "")
    if config.DASH_KEY and k:
        import hmac as _h
        if _h.compare_digest(k, config.DASH_KEY):  # admin bookmark: swap the key for a cookie, clean the URL
            return _signed_in(RedirectResponse("/", status_code=303), "owner")
        return _denied()
    if not _owner(request):
        return HTMLResponse(landing_page())
    return timeline_page()


@app.get("/lives/{life_id}", response_class=HTMLResponse)
def lives(life_id: int, request: Request):
    if not _owner(request):
        return _denied()
    return life_page(life_id)


@app.post("/undo/{run_id}")
def undo(run_id: int, request: Request):
    if not _owner(request):
        return _denied()
    with RUN_LOCK:
        undone, restore = undo_run(run_id)
    if restore:
        checkpoint_restore(restore)
    return RedirectResponse(f"/?undone={run_id}" + (f"&restored={restore}" if restore else ""), status_code=303)


@app.get("/u/{tenant}", response_class=HTMLResponse)
def tenant_home(tenant: str, request: Request):
    if not _can_view(request, tenant):
        return _denied()
    return timeline_page(tenant)


@app.get("/u/{tenant}/lives/{life_id}", response_class=HTMLResponse)
def tenant_lives(tenant: str, life_id: int, request: Request):
    if not _can_view(request, tenant):
        return _denied()
    return life_page(life_id, tenant)


@app.post("/u/{tenant}/undo/{run_id}")
def tenant_undo(tenant: str, run_id: int, request: Request):
    if not _can_view(request, tenant):
        return _denied()
    if not db.one("SELECT 1 FROM runs WHERE id=? AND tenant=?", run_id, tenant):
        raise HTTPException(404, "no such run")
    with RUN_LOCK:
        undone, restore = undo_run(run_id)
    if restore:
        checkpoint_restore(restore)
    return RedirectResponse(f"/u/{tenant}", status_code=303)


@app.get("/api/state")
def state(request: Request):
    if not _owner(request):
        return _denied()
    return JSONResponse({
        "runs": [dict(r) for r in db.q("SELECT * FROM runs ORDER BY id DESC LIMIT 50")],
        "actions": [dict(r) for r in db.q("SELECT * FROM actions ORDER BY id DESC LIMIT 200")],
        "reminders": [dict(r) for r in db.q("SELECT * FROM reminders ORDER BY due_at")],
    })
