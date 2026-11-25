"""Parallel Lives: one throwaway Sprite per option. Each one lives that future and reports back."""
import json
import logging
import re
import threading
from datetime import datetime
from pathlib import Path

from . import config, db
from .gw import gemini_text, mail, memory
from .sprite import checkpoint_create, keep_awake, sprites
from .tools import TZ, Run

log = logging.getLogger("cc.lives")
WORKER = (Path(__file__).parent / "life_worker.py").read_bytes()
LABELS = ["A", "B", "C"]


def start_lives(run: Run, msg: dict) -> None:
    req = run.lives_request
    threading.Thread(target=_orchestrate, args=(run, msg, req), daemon=True).start()


def _alive_timelines() -> list:
    return db.q("SELECT * FROM timelines WHERE status IN ('creating','living','reported','chosen')")


def _orchestrate(run: Run, msg: dict, req: dict) -> None:
    with keep_awake(30):
        if not sprites.ready:
            mail.reply(run.message_id, "Parallel Lives isn't switched on yet (I need a Sprites API token to spin up "
                       "timeline computers). Once it's set, ask me again!")
            return
        # Free plan = 3 Sprites max, so collapse any older timelines first.
        for t in _alive_timelines():
            try:
                sprites.destroy(t["sprite_name"])
            except Exception:
                log.exception("destroy old timeline")
            db.ex("UPDATE timelines SET status='collapsed' WHERE id=?", t["id"])

        life_id = db.ex("INSERT INTO lives(run_id, created_at, question, status, thread_id, message_id, recipient, "
                        "tenant) VALUES (?,?,?,?,?,?,?,?)", run.id, db.now(), req["question"], "living",
                        run.thread_id, run.message_id, run.sender, run.tenant)
        opts = req["options"]
        threads, results = [], {}
        for i, option in enumerate(opts):
            label = LABELS[i]
            name = f"cc-life-{life_id}-{label.lower()}"
            tid = db.ex("INSERT INTO timelines(life_id, label, option, sprite_name, status) VALUES (?,?,?,?,?)",
                        life_id, label, option, name, "creating")
            db.add_action(run.id, "timeline", f"Created timeline Sprite {name} ({option})",
                          {"sprite_name": name, "timeline_id": tid})
            brief = {"question": req["question"], "option": option, "label": label,
                     "others": [o for o in opts if o != option], "context": req.get("context", ""),
                     "today": datetime.now(TZ).strftime("%B %d, %Y"), "recipient": run.sender,
                     "inbox": config.INBOX}
            th = threading.Thread(target=_live_one, args=(tid, name, brief, results), daemon=True)
            th.start()
            threads.append(th)
        for th in threads:
            th.join(timeout=900)

        rows = db.q("SELECT * FROM timelines WHERE life_id=? ORDER BY label", life_id)
        reports = [(r, json.loads(r["report"])) for r in rows if r["report"]]
        if not reports:
            db.ex("UPDATE lives SET status='failed' WHERE id=?", life_id)
            mail.reply(run.message_id, "Both timelines collapsed before reporting back (the AI model was overloaded). "
                       "Try me again in a few minutes.")
            return
        summary = _compare(req, reports)
        db.ex("UPDATE lives SET status='reported', summary=? WHERE id=?", summary, life_id)
        mail.reply(run.message_id, summary + f"\n\n--\n{config.AGENT_NAME} · "
                   f"{config.tenant_link(run.tenant, f'/lives/{life_id}')}")


def _live_one(tid: int, name: str, brief: dict, results: dict) -> None:
    try:
        sprites.create(name)
        db.ex("UPDATE timelines SET status='living' WHERE id=?", tid)
        sprites.write_file(name, "/home/sprite/life/life_worker.py", WORKER)
        sprites.write_file(name, "/home/sprite/life/brief.json", json.dumps(brief).encode())
        out = sprites.exec(name, ["python3", "/home/sprite/life/life_worker.py"], timeout=900)
        m = re.search(r"===LIFE_REPORT_BEGIN===\s*(\{.*\})\s*===LIFE_REPORT_END===", out, re.S)
        report = json.loads(m.group(1)) if m else json.loads(sprites.read_file(name, "/home/sprite/life/report.json"))
        if "error" in report and "headline" not in report:
            raise RuntimeError(report["error"])
        db.ex("UPDATE timelines SET status='reported', report=? WHERE id=?", json.dumps(report), tid)
    except Exception as e:
        log.exception("timeline %s failed", name)
        db.ex("UPDATE timelines SET status='failed', error=? WHERE id=?", str(e)[:500], tid)


def _budget(rep: dict) -> float:
    return sum(float(b.get("usd") or 0) for b in rep.get("monthly_budget", []))


def _compare(req: dict, reports: list) -> str:
    blocks = []
    for row, rep in reports:
        blocks.append(
            f"TIMELINE {row['label']}: {row['option']}   (lived on Sprite {row['sprite_name']})\n"
            f"  {rep.get('headline', '')}\n"
            f"  Happiness {rep.get('happiness')}/10 · Growth {rep.get('growth')}/10 · "
            f"Money {rep.get('financial_health')}/10 · ~${_budget(rep):,.0f}/month\n"
            f"  + {('; '.join(rep.get('wins', [])[:2]))}\n"
            f"  - {('; '.join(rep.get('hardships', [])[:2]))}")
    raw = "\n\n".join(blocks)
    try:
        verdict = gemini_text(
            f"Decision: {req['question']}\nContext: {req.get('context', '')}\n\n{raw}\n\n"
            "In 3-4 plain sentences, compare these timelines honestly and say which one you'd lean toward and why, "
            "plus the one question the person should answer for themselves before deciding. No markdown.",
            "You are a wise, candid chief of staff.")
    except Exception:
        verdict = "I couldn't reach the model for a verdict, but the numbers above tell the story."
    letters = "You should also have a letter from each timeline in your inbox: written from inside that future."
    return (f"I split reality {len(reports)} ways for \"{req['question']}\". Each option got its own computer "
            f"that lived one year of that future.\n\n{raw}\n\nMy take: {verdict.strip()}\n\n{letters}\n\n"
            f"Reply \"choose A\" or \"choose B\" and I'll collapse the other timeline (its Sprite gets destroyed) "
            f"and send you the first steps for the life you picked.")


def handle_choice(msg: dict, sender: str, letter: str, tenant: str) -> None:
    letter = {"1": "A", "2": "B"}.get(letter, letter.upper())
    life = (db.one("SELECT * FROM lives WHERE thread_id=? AND tenant=? AND status='reported' ORDER BY id DESC LIMIT 1",
                   msg.get("thread_id"), tenant)
            or db.one("SELECT * FROM lives WHERE tenant=? AND status='reported' ORDER BY id DESC LIMIT 1", tenant))
    if not life:
        mail.reply(msg["message_id"], "There's no open decision to choose from right now.")
        return
    rows = db.q("SELECT * FROM timelines WHERE life_id=?", life["id"])
    winner = next((r for r in rows if r["label"] == letter and r["report"]), None)
    if not winner:
        mail.reply(msg["message_id"], f"I don't have a Timeline {letter} for that decision.")
        return
    cp = checkpoint_create(f"before choosing a timeline (decision #{life['id']})")
    run_id = db.ex("INSERT INTO runs(created_at, message_id, thread_id, sender, subject, mode, checkpoint, status, "
                   "tenant) VALUES (?,?,?,?,?,?,?,?,?)", db.now(), msg["message_id"], msg.get("thread_id"), sender,
                   f"Chose timeline {letter}: {winner['option']}", "owner" if tenant == "owner" else "guest",
                   cp or "", "done", tenant)
    collapsed = []
    for r in rows:
        if r["id"] == winner["id"]:
            continue
        try:
            sprites.destroy(r["sprite_name"])
        except Exception:
            log.exception("destroy loser")
        db.ex("UPDATE timelines SET status='collapsed' WHERE id=?", r["id"])
        collapsed.append(f"Timeline {r['label']} ({r['option']}): Sprite {r['sprite_name']} destroyed")
    db.ex("UPDATE timelines SET status='chosen' WHERE id=?", winner["id"])
    db.ex("UPDATE lives SET status='chosen', chosen=? WHERE id=?", letter, life["id"])
    rep = json.loads(winner["report"])
    rid = f"decision-{life['id']}"
    memory.upsert([{"_id": rid, "text": f"Decided '{life['question']}': chose {winner['option']} "
                    f"(on {datetime.now(TZ):%b %d, %Y}).", "category": "general", "created_at": db.now(),
                    "source": "Parallel Lives"}], ns=f"mem-{tenant}")
    db.add_action(run_id, "memory", f"Remembered decision: {winner['option']}", {"ids": [rid], "ns": f"mem-{tenant}"})
    steps = "\n".join(f"- {s}" for s in rep.get("first_steps", []))
    mail.reply(msg["message_id"],
               f"Timeline {letter} it is: {winner['option']}. 🌱\n\n"
               + "\n".join(f"- collapsed {c}" for c in collapsed)
               + f"\n\nYour first steps this week:\n{steps}\n\n"
                 f"I'll remember this decision. Timeline {letter}'s Sprite ({winner['sprite_name']}) stays asleep as an "
                 f"archive of that future until your next big decision."
               + f"\n\n--\n{config.AGENT_NAME} · {config.tenant_link(tenant, f'/lives/{life['id']}')}")
