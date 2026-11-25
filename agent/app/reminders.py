"""Due reminders go out whenever the Sprite is awake: on every email and on the external /tick ping."""
import logging
import threading
from datetime import datetime, timezone

from . import config, db
from .gw import mail
from .tools import reminder_text

log = logging.getLogger("cc.reminders")
_lock = threading.Lock()


def send_due() -> int:
    if not _lock.acquire(blocking=False):
        return 0
    sent = 0
    try:
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        for r in db.q("SELECT m.*, r.tenant FROM reminders m JOIN runs r ON r.id=m.run_id "
                      "WHERE m.status='pending' AND m.due_at <= ? ORDER BY m.due_at", now):
            text = reminder_text(r["text"], r["tenant"] or "owner")
            try:
                if r["message_id"]:
                    mail.reply(r["message_id"], text)
                elif r["recipient"]:
                    mail.send([r["recipient"]], f"Reminder: {r['text'][:60]}", text)
                db.ex("UPDATE reminders SET status='sent' WHERE id=?", r["id"])
                sent += 1
            except Exception:
                log.exception("reminder %s failed", r["id"])
    finally:
        _lock.release()
    return sent
