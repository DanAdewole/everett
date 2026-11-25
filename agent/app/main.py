"""HTTP surface of the Sprite: AgentMail webhook, timeline dashboard, undo buttons, reminder tick."""
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
from .reminders import send_due
from .sprite import checkpoint_restore, keep_awake

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


def _authed(request: Request) -> None:
    if config.DASH_KEY and request.query_params.get("k") != config.DASH_KEY:
        raise HTTPException(403, "missing or wrong key")


def _work(event: dict) -> None:
    with keep_awake(20):
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
    # Ack fast (AgentMail retries slow webhooks); a keep-awake task stops the Sprite sleeping mid-work.
    threading.Thread(target=_work, args=(event,), daemon=True).start()
    return {"ok": True}


@app.api_route("/tick", methods=["GET", "POST"])
def tick():
    """External cron pings this so reminders fire even while no email arrives."""
    return {"ok": True, "reminders_sent": send_due()}


@app.get("/health")
def health():
    return {"ok": True, "agent": config.AGENT_NAME}


@app.get("/", response_class=HTMLResponse)
def home(request: Request):
    if config.DASH_KEY and request.query_params.get("k") != config.DASH_KEY:
        return HTMLResponse(f"<h1>{config.AGENT_NAME}</h1><p>Email me: {config.INBOX}</p>", status_code=200)
    return timeline_page()


@app.get("/lives/{life_id}", response_class=HTMLResponse)
def lives(life_id: int, request: Request):
    _authed(request)
    return life_page(life_id)


@app.post("/undo/{run_id}")
def undo(run_id: int, request: Request):
    _authed(request)
    with RUN_LOCK:
        undone, restore = undo_run(run_id)
    if restore:
        checkpoint_restore(restore)
    return RedirectResponse(config.dash_link("/") + f"&undone={run_id}" + (f"&restored={restore}" if restore else ""),
                            status_code=303)


def _tenant_ok(tenant: str, request: Request) -> None:
    import hmac as _h
    if not _h.compare_digest(request.query_params.get("t", ""), config.tenant_token(tenant)):
        raise HTTPException(403, "bad link")


@app.get("/u/{tenant}", response_class=HTMLResponse)
def tenant_home(tenant: str, request: Request):
    _tenant_ok(tenant, request)
    return timeline_page(tenant)


@app.get("/u/{tenant}/lives/{life_id}", response_class=HTMLResponse)
def tenant_lives(tenant: str, life_id: int, request: Request):
    _tenant_ok(tenant, request)
    return life_page(life_id, tenant)


@app.post("/u/{tenant}/undo/{run_id}")
def tenant_undo(tenant: str, run_id: int, request: Request):
    _tenant_ok(tenant, request)
    if not db.one("SELECT 1 FROM runs WHERE id=? AND tenant=?", run_id, tenant):
        raise HTTPException(404, "no such run")
    with RUN_LOCK:
        undone, restore = undo_run(run_id)
    if restore:
        checkpoint_restore(restore)
    return RedirectResponse(config.tenant_link(tenant), status_code=303)


@app.get("/api/state")
def state(request: Request):
    _authed(request)
    return JSONResponse({
        "runs": [dict(r) for r in db.q("SELECT * FROM runs ORDER BY id DESC LIMIT 50")],
        "actions": [dict(r) for r in db.q("SELECT * FROM actions ORDER BY id DESC LIMIT 200")],
        "reminders": [dict(r) for r in db.q("SELECT * FROM reminders ORDER BY due_at")],
    })
