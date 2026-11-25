"""Thin clients for every connector. All calls go through the Sprites gateway: no API keys here."""
import json
import logging
import time
from urllib.parse import quote

import httpx

from . import config

log = logging.getLogger("cc.gw")
http = httpx.Client(timeout=httpx.Timeout(90, connect=15), follow_redirects=True)
_bases: dict[str, str] = {}


def _load_bases() -> None:
    r = http.get("https://api.sprites.dev/v1/gateway/list", timeout=20)
    r.raise_for_status()
    for c in r.json().get("connections", []):
        _bases.setdefault(c["provider"], c["gateway_base_url"])
        if c["provider"] == "custom_api":
            # Custom API connections are keyed by what they wrap, e.g. "custom_api:agentmail".
            label = " ".join(str(c.get(k) or "") for k in ("display_name", "description", "provider_account_name"))
            for word in ("agentmail",):
                if word in label.lower():
                    _bases.setdefault(f"custom_api:{word}", c["gateway_base_url"])


def base(provider: str) -> str:
    if provider not in _bases:
        _load_bases()
    if provider not in _bases:
        raise RuntimeError(f"connector '{provider}' is not connected for this Sprite")
    return _bases[provider]


def has(provider: str) -> bool:
    try:
        base(provider)
        return True
    except Exception:
        return False


def _check(r: httpx.Response) -> httpx.Response:
    if r.status_code >= 400:
        raise RuntimeError(f"{r.request.method} {r.request.url.path} -> {r.status_code}: {r.text[:400]}")
    return r


# ---------------------------------------------------------------- AgentMail
class Mail:
    def __init__(self, inbox: str = config.INBOX):
        self.inbox = inbox

    def _u(self, path: str, provider: str = "agentmail") -> str:
        return f"{base(provider)}/v0/inboxes/{quote(self.inbox, safe='')}{path}"

    @staticmethod
    def _id(message_id: str) -> str:
        """Double-encode: the gateway decodes once and rejects '=' (common in Gmail Message-IDs);
        AgentMail decodes the second layer."""
        return quote(quote(message_id, safe=""), safe="")

    def get_message(self, message_id: str) -> dict:
        return _check(http.get(self._u(f"/messages/{self._id(message_id)}"))).json()

    def get_thread(self, thread_id: str) -> dict:
        return _check(http.get(self._u(f"/threads/{quote(thread_id, safe='')}"), params={"limit": 20})).json()

    def reply(self, message_id: str, text: str, html: str | None = None, attachments: list | None = None) -> dict:
        body = {"text": text}
        if html:
            body["html"] = html
        if attachments:
            body["attachments"] = attachments
        return _check(http.post(self._u(f"/messages/{self._id(message_id)}/reply"), json=body)).json()

    def send(self, to: list[str], subject: str, text: str, html: str | None = None) -> dict:
        body = {"to": to, "subject": subject, "text": text}
        if html:
            body["html"] = html
        return _check(http.post(self._u("/messages/send"), json=body)).json()

    def schedule_reply(self, message_id: str, text: str, send_at_utc: str) -> str:
        """A reply draft that AgentMail itself sends at send_at, so reminders fire while this Sprite sleeps."""
        d = _check(http.post(self._u("/drafts"), json={"in_reply_to": message_id, "text": text,
                                                       "send_at": send_at_utc})).json()
        return d["draft_id"]

    def delete_draft(self, draft_id: str) -> bool:
        """False if the draft is gone already (e.g. AgentMail has sent it)."""
        r = http.delete(self._u(f"/drafts/{quote(draft_id, safe='')}"))
        if r.status_code == 404:
            return False
        _check(r)
        return True

    def attachment_bytes(self, message_id: str, attachment_id: str) -> tuple[dict, bytes]:
        # The AgentMail connector blocks attachment endpoints, so downloads go through a Custom API connector
        # wrapping api.agentmail.to (the key still lives in Fly's vault, never on this Sprite).
        provider = "custom_api:agentmail" if has("custom_api:agentmail") else "agentmail"
        r = None
        for mid in (self._id(message_id), quote(message_id, safe="")):  # gateways may decode once or not at all
            r = http.get(self._u(f"/messages/{mid}/attachments/{quote(attachment_id, safe='')}", provider))
            if r.status_code < 400:
                break
        meta = _check(r).json()
        data = _check(http.get(meta["download_url"])).content
        return meta, data

    def label(self, message_id: str, add: list[str]) -> None:
        try:
            http.patch(self._u(f"/messages/{self._id(message_id)}"), json={"add_labels": add})
        except Exception as e:  # labels are cosmetic
            log.warning("label failed: %s", e)

    def list_webhooks(self) -> list[dict]:
        return _check(http.get(self._u("/webhooks"))).json().get("webhooks", [])

    def create_webhook(self, url: str) -> dict:
        return _check(http.post(self._u("/webhooks"), json={"url": url, "event_types": ["message.received"]})).json()

    def delete_webhook(self, webhook_id: str) -> None:
        _check(http.delete(self._u(f"/webhooks/{quote(webhook_id, safe='')}")))


mail = Mail()


# ---------------------------------------------------------------- Box
class Box:
    _folder_cache: dict[tuple[str, str], str] = {}

    def _u(self, path: str) -> str:
        return f"{base('box')}{path}"

    def folder(self, name: str, parent: str = "0") -> str:
        key = (parent, name.lower())
        if key in self._folder_cache:
            return self._folder_cache[key]
        r = http.post(self._u("/2.0/folders"), json={"name": name, "parent": {"id": parent}})
        if r.status_code == 409:
            fid = r.json()["context_info"]["conflicts"][0]["id"]
        else:
            fid = _check(r).json()["id"]
        self._folder_cache[key] = fid
        return fid

    def path(self, *parts: str) -> str:
        fid = "0"
        for p in parts:
            fid = self.folder(p, fid)
        return fid

    def upload(self, folder_id: str, name: str, data: bytes, content_type: str = "application/octet-stream") -> dict:
        stem, dot, ext = name.rpartition(".")
        if not dot:
            stem, ext = name, ""
        for i in range(6):
            candidate = name if i == 0 else f"{stem} ({i + 1}){'.' + ext if ext else ''}"
            files = {
                "attributes": (None, json.dumps({"name": candidate, "parent": {"id": folder_id}}), "application/json"),
                "file": (candidate, data, content_type),
            }
            r = http.post(self._u("/upload/files/content"), files=files)
            if r.status_code == 409:
                continue
            entry = _check(r).json()["entries"][0]
            return {"id": entry["id"], "name": entry["name"], "url": f"https://app.box.com/file/{entry['id']}"}
        raise RuntimeError(f"could not upload {name}: name conflicts")

    def retire_file(self, file_id: str, name: str, note: str) -> None:
        """Undo a filing. The Box connector forbids delete/move/edit but allows uploading a new version, so the
        file is replaced by a short note and renamed '(undone) ...'; the original stays in Box version history."""
        new_name = f"(undone) {name.rsplit('.', 1)[0]}.txt"
        files = {"attributes": (None, json.dumps({"name": new_name}), "application/json"),
                 "file": (new_name, note.encode(), "text/plain")}
        r = http.post(self._u(f"/upload/files/{file_id}/content"), files=files)
        if r.status_code != 404:
            _check(r)


box = Box()


# ---------------------------------------------------------------- Pinecone (records API, integrated embedding)
class Memory:
    H = {"X-Pinecone-API-Version": "2025-04"}

    def _u(self, path: str) -> str:
        return f"{base('pinecone')}/indexes/{config.PINECONE_INDEX}{path}"

    def upsert(self, records: list[dict], ns: str = config.PINECONE_NS) -> None:
        body = "\n".join(json.dumps(r) for r in records)
        _check(http.post(self._u(f"/records/namespaces/{ns}/upsert"), content=body,
                         headers={**self.H, "Content-Type": "application/x-ndjson"}))

    def search(self, query: str, k: int = 6, ns: str = config.PINECONE_NS) -> list[dict]:
        r = http.post(self._u(f"/records/namespaces/{ns}/search"), headers=self.H, json={
            "query": {"inputs": {"text": query}, "top_k": k},
            "fields": ["text", "category", "created_at", "source"],
        })
        if r.status_code == 404:  # namespace not created yet
            return []
        hits = _check(r).json().get("result", {}).get("hits", [])
        return [{"id": h["_id"], "score": round(h["_score"], 3), **h.get("fields", {})} for h in hits]

    def delete(self, ids: list[str], ns: str = config.PINECONE_NS) -> None:
        if ids:
            _check(http.post(self._u("/vectors/delete"), json={"namespace": ns, "ids": ids}))

    def wipe(self, ns: str) -> None:
        r = http.post(self._u("/vectors/delete"), json={"namespace": ns, "deleteAll": True})
        if r.status_code != 404:  # an empty namespace is already wiped
            _check(r)


memory = Memory()


# ---------------------------------------------------------------- Gemini
RETRYABLE = {408, 429, 500, 502, 503, 504}
# Circuit breaker: a model that just hung or said "overloaded" is benched for a while, so the next call goes
# straight to a healthy one instead of waiting out another timeout (free-tier flash models flap a lot).
_benched: dict[str, float] = {}
BENCH_SECONDS = 600


def _bench(model: str, why: str) -> None:
    _benched[model] = time.time() + BENCH_SECONDS
    log.warning("gemini %s benched for %ds (%s)", model, BENCH_SECONDS, why[:80])


def _strip_signatures(contents: list[dict]) -> list[dict]:
    """Hand a conversation to a different model. Signatures are model-specific, but newer models require one on
    every functionCall part, so swap in Gemini's documented placeholder for transferred traces."""
    out = []
    for c in contents:
        parts = []
        for p in c.get("parts", []):
            p = {k: v for k, v in p.items() if k != "thoughtSignature"}
            if "functionCall" in p:
                p["thoughtSignature"] = "skip_thought_signature_validator"
            parts.append(p)
        out.append({**c, "parts": parts})
    return out


def gemini(contents: list[dict], system: str, tools: list[dict] | None = None,
           prefer: str | None = None, json_schema: dict | None = None, rounds: int = 4) -> tuple[str, dict]:
    """Call Gemini with a model fallback chain. Returns (model_used, response_json)."""
    models = list(config.GEMINI_MODELS)
    if prefer in models:
        models.remove(prefer)
        models.insert(0, prefer)
    body: dict = {"contents": contents, "systemInstruction": {"parts": [{"text": system}]}}
    if tools:
        body["tools"] = [{"functionDeclarations": tools}]
    if json_schema:
        body["generationConfig"] = {"responseMimeType": "application/json", "responseSchema": json_schema}
    last = ""
    for attempt in range(rounds):
        now = time.time()
        healthy = [m for m in models if _benched.get(m, 0) < now]
        for m in healthy or models:  # if everything is benched, try them all anyway
            payload = body if (m == prefer or prefer is None) else {**body, "contents": _strip_signatures(contents)}
            try:
                r = http.post(f"{base('gemini')}/v1beta/models/{m}:generateContent", json=payload,
                              timeout=httpx.Timeout(30, connect=10))
            except httpx.HTTPError as e:
                last = f"{m}: {e}"
                _bench(m, f"{type(e).__name__}")
                continue
            if r.status_code == 200:
                data = r.json()
                if data.get("candidates"):
                    return m, data
                last = f"{m}: empty candidates {str(data)[:200]}"
                continue
            last = f"{m}: {r.status_code} {r.text[:200]}"
            if r.status_code in (429, 503, 504):
                _bench(m, f"HTTP {r.status_code}")
            if r.status_code not in RETRYABLE and r.status_code != 404:
                log.warning("gemini non-retryable %s", last)
        time.sleep(min(30, 3 * 2 ** attempt))
    raise RuntimeError(f"all Gemini models failed: {last}")


def gemini_text(prompt: str, system: str, json_schema: dict | None = None) -> str:
    _, data = gemini([{"role": "user", "parts": [{"text": prompt}]}], system, json_schema=json_schema)
    parts = data["candidates"][0].get("content", {}).get("parts", [])
    return "".join(p.get("text", "") for p in parts if not p.get("thought"))
