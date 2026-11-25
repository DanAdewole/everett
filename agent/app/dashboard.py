"""Server-rendered timeline page: every run, every action, every checkpoint, with undo buttons."""
import html
import json
from datetime import datetime

from . import config, db
from .sprite import checkpoint_list
from .tools import fmt_local, tenant_tz

CSS = """
:root{--bg:#f7f5f0;--card:#fff;--ink:#1d1b18;--mute:#6f6a62;--line:#e6e1d8;--accent:#7c3aed;--ok:#15803d;--warn:#b45309;--bad:#b91c1c}
@media (prefers-color-scheme:dark){:root{--bg:#15141a;--card:#1e1d25;--ink:#ece9f2;--mute:#9b97a6;--line:#2e2c38;--accent:#a78bfa;--ok:#4ade80;--warn:#fbbf24;--bad:#f87171}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.5 ui-sans-serif,system-ui,-apple-system,"Segoe UI",sans-serif}
main{max-width:980px;margin:0 auto;padding:28px 16px 64px}h1{font-size:26px;margin:0}h2{font-size:15px;text-transform:uppercase;letter-spacing:.06em;color:var(--mute);margin:32px 0 12px}
.sub{color:var(--mute);margin:4px 0 0}.grid{display:grid;grid-template-columns:2fr 1fr;gap:20px}@media(max-width:760px){.grid{grid-template-columns:1fr}}
.card{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:14px 16px;margin-bottom:12px}
.row{display:flex;justify-content:space-between;gap:12px;align-items:flex-start}.meta{color:var(--mute);font-size:13px}
.pill{display:inline-block;font-size:12px;padding:1px 8px;border-radius:99px;border:1px solid var(--line);color:var(--mute);margin-right:6px}
.pill.cp{color:var(--accent);border-color:var(--accent)}.pill.done{color:var(--ok);border-color:var(--ok)}.pill.undone{color:var(--warn);border-color:var(--warn);text-decoration:line-through}.pill.error,.pill.blocked{color:var(--bad);border-color:var(--bad)}
ul{margin:8px 0 0;padding-left:18px}li.undone{text-decoration:line-through;color:var(--mute)}
button{background:var(--accent);color:#fff;border:0;border-radius:9px;padding:7px 12px;font-weight:600;cursor:pointer}button:hover{filter:brightness(1.1)}
.banner{background:var(--card);border:1px solid var(--accent);border-radius:12px;padding:10px 14px;margin-top:16px}
.cps li{font-size:13px;margin-bottom:4px}.tl{display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:16px}
.tm{overflow-x:auto;padding:4px 2px 2px;-webkit-overflow-scrolling:touch}.track{display:flex;min-width:max-content}
.node{flex:0 0 170px;position:relative;padding:28px 14px 4px 0}
.node:before{content:"";position:absolute;top:10px;left:0;right:0;height:2px;background:var(--line)}
.node:first-child:before{left:12px}.node.now:before{right:auto;width:14px}
.dot{position:absolute;top:2px;left:4px;width:18px;height:18px;border-radius:50%;background:var(--accent);border:3px solid var(--card);box-shadow:0 0 0 1px var(--line)}
.node.undone .dot{background:var(--warn)}.node.undone .c{text-decoration:line-through;color:var(--mute)}
.node.rewind .dot{background:var(--warn);border-radius:4px}.node.rewind .v{color:var(--warn)}
.node.now .dot{background:var(--ok);animation:pulse 2s infinite}@keyframes pulse{50%{box-shadow:0 0 0 6px transparent,0 0 0 1px var(--line)}}
.node .v{font-weight:700;font-size:14px}.node .t{font-size:12px;color:var(--mute)}.node .c{font-size:13px;margin-top:2px}
pre{white-space:pre-wrap;font:inherit;margin:8px 0 0}.score{font-size:13px;color:var(--mute)}a{color:var(--accent)}
"""


def _page(title: str, body: str) -> str:
    return (f"<!doctype html><html><head><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'>"
            f"<title>{html.escape(title)}</title><style>{CSS}</style></head><body><main>{body}</main></body></html>")


import threading

_view = threading.local()  # timezone of the page being rendered (pages render on worker threads)


def _local(iso: str | None) -> str:
    if not iso:
        return ""
    try:
        return fmt_local(datetime.fromisoformat(iso.replace("Z", "+00:00")), _view.tz, "%b %d, %I:%M %p")
    except ValueError:
        return iso


def _link(tenant: str | None, path: str) -> str:
    """tenant=None is the owner's admin view (sees everything)."""
    if tenant is None:
        return path  # auth is an HttpOnly session cookie now: no secrets in links
    return f"/u/{tenant}{path}"


def _ts(iso: str | None) -> datetime:
    """Parse ISO times, including the nanosecond ones `sprite-env` prints."""
    import re
    iso = (iso or "1970-01-01T00:00:00+00:00").replace("Z", "+00:00")
    iso = re.sub(r"(\.\d{6})\d+", r"\1", iso)
    try:
        return datetime.fromisoformat(iso)
    except ValueError:
        return datetime.fromisoformat("1970-01-01T00:00:00+00:00")


def _time_machine(tenant: str | None) -> str:
    """Checkpoints as a track you can read left to right: each one protected a run; rewinds jump back."""
    import re
    e = html.escape
    runs = {r["checkpoint"]: r for r in db.q("SELECT * FROM runs WHERE checkpoint IS NOT NULL AND checkpoint != '' "
                                             "AND (? IS NULL OR tenant=?)", tenant, tenant)}
    events = []
    for c in checkpoint_list():
        r = runs.get(c["id"])
        comment = c.get("comment") or ""
        if r is None and (tenant is not None or comment.startswith("pre-restore")):
            continue  # guests only see their own history; pre-restore snapshots are plumbing
        label = (f"before #{r['id']} · {r['subject'] or ''}" if r else comment) or "checkpoint"
        undone = bool(r and r["status"] == "undone")
        events.append((_ts(c.get("create_time")), "undone" if undone else "", c["id"], label[:60]))
    for x in db.q("SELECT * FROM restores WHERE (? IS NULL OR tenant=?)", tenant, tenant):
        events.append((_ts(x["at"]), "rewind", f"⏪ {x['version']}",
                       f"undo of #{x['run_id']}: rewound the whole computer to {x['version']}"))
    if not events:
        return ""
    events.sort(key=lambda ev: ev[0])
    nodes = "".join(f"<div class='node {cls}'><span class=dot></span><div class=v>{e(v)}</div>"
                    f"<div class=t>{_local(t.isoformat())}</div><div class=c>{e(lbl)}</div></div>"
                    for t, cls, v, lbl in events[-14:])
    nodes += "<div class='node now'><span class=dot></span><div class=v>now</div><div class=c>live</div></div>"
    return (f"<h2>Time machine</h2><div class=card><div class=tm><div class=track>{nodes}</div></div>"
            f"<div class=meta style='margin-top:8px'>Every dot is a full snapshot of this computer, taken before "
            f"an action. ⏪ marks a moment it was rewound. Struck-through runs were undone.</div></div>"
            "<script>document.querySelectorAll('.tm').forEach(t=>t.scrollLeft=t.scrollWidth)</script>")


def timeline_page(tenant: str | None = None) -> str:
    e = html.escape
    _view.tz = tenant_tz(tenant)
    if tenant is None:
        runs = db.q("SELECT * FROM runs ORDER BY id DESC LIMIT 60")
    else:
        runs = db.q("SELECT * FROM runs WHERE tenant=? ORDER BY id DESC LIMIT 40", tenant)
    cards = []
    for r in runs:
        acts = db.q("SELECT * FROM actions WHERE run_id=? ORDER BY id", r["id"])
        live = [a for a in acts if not a["undone"]]
        items = "".join(f"<li class='{'undone' if a['undone'] else ''}'>{e(a['summary'])}</li>" for a in acts)
        btn = (f"<form method=post action='{_link(tenant, f'/undo/{r["id"]}')}' "
               f"onsubmit=\"return confirm('Undo run #{r['id']}?')\"><button>Undo</button></form>"
               if live and r["status"] == "done" else "")
        cp = f"<span class='pill cp'>checkpoint {e(r['checkpoint'])}</span>" if r["checkpoint"] else ""
        cards.append(
            f"<div class=card><div class=row><div><b>#{r['id']} · {e(r['subject'] or '')}</b>"
            f"<div class=meta>{_local(r['created_at'])} · {e(r['mode'] or '')}"
            f"{' · from ' + e(r['sender'] or '') if tenant is None else ''}</div>"
            f"<div style='margin-top:6px'><span class='pill {e(r['status'] or '')}'>{e(r['status'] or '')}</span>{cp}"
            f"{'<span class=pill>' + e(r['model']) + '</span>' if r['model'] else ''}</div></div>{btn}</div>"
            f"{'<div class=meta>' + ('⚠️ failed authentication: ' if r['status'] == 'blocked' else '⏪ ') + e(r['error']) + '</div>' if r['status'] in ('blocked', 'undone') and r['error'] else ''}"
            f"{'<div class=meta>🛡️ quarantined: ' + e(r['flags']) + '</div>' if r['flags'] else ''}"
            f"{'<ul>' + items + '</ul>' if items else ''}</div>")
    rems = db.q(f"SELECT m.* FROM reminders m JOIN runs r ON r.id=m.run_id WHERE {db.UPCOMING} "
                "AND (? IS NULL OR r.tenant=?) ORDER BY m.due_at LIMIT 15", tenant, tenant)
    rem_items = "".join(f"<li>{_local(r['due_at'])}: {e(r['text'])}</li>" for r in rems) or "<li class=meta>none</li>"
    lives = db.q("SELECT * FROM lives WHERE (? IS NULL OR tenant=?) ORDER BY id DESC LIMIT 5", tenant, tenant)
    life_items = "".join(f"<li><a href='{_link(tenant, f'/lives/{l["id"]}')}'>{e(l['question'])}</a> "
                         f"<span class=meta>({e(l['status'])}{', chose ' + e(l['chosen']) if l['chosen'] else ''})</span></li>"
                         for l in lives) or "<li class=meta>none yet: email me \"help me decide: X or Y\"</li>"
    body = f"""
<h1>{e(config.AGENT_NAME)}'s timeline{'' if tenant is None or tenant == 'owner' else ' (your private space)'}</h1>
<p class=sub>Email <b>{e(config.INBOX)}</b>. Every action is preceded by a checkpoint of this whole computer. Undo rewinds it.</p>
{_time_machine(tenant)}
<div class=grid><section><h2>Runs</h2>{''.join(cards) or '<div class=card>No emails yet.</div>'}</section>
<aside><h2>Upcoming reminders</h2><div class=card><ul>{rem_items}</ul></div>
<h2>Parallel lives</h2><div class=card><ul>{life_items}</ul></div></aside></div>"""
    return _page(f"{config.AGENT_NAME} timeline", body)


def life_page(life_id: int, tenant: str | None = None) -> str:
    e = html.escape
    _view.tz = tenant_tz(tenant)
    life = db.one("SELECT * FROM lives WHERE id=? AND (? IS NULL OR tenant=?)", life_id, tenant, tenant)
    if not life:
        return _page("Not found", "<h1>No such decision</h1>")
    cols = []
    for t in db.q("SELECT * FROM timelines WHERE life_id=? ORDER BY label", life_id):
        rep = json.loads(t["report"]) if t["report"] else {}
        budget = "".join(f"<li>{e(b['item'])}: ${float(b['usd']):,.0f}</li>" for b in rep.get("monthly_budget", []))
        status = {"chosen": "done", "collapsed": "undone", "failed": "error"}.get(t["status"], "")
        cols.append(f"""<div class=card><span class='pill {status}'>{e(t['status'])}</span><span class='pill cp'>{e(t['sprite_name'])}</span>
<h3 style='margin:8px 0 2px'>Timeline {e(t['label'])}: {e(t['option'])}</h3><div class=meta>{e(rep.get('headline', t['error'] or ''))}</div>
<p class=score>Happiness {rep.get('happiness', '–')}/10 · Growth {rep.get('growth', '–')}/10 · Money {rep.get('financial_health', '–')}/10</p>
<b>One year later</b><pre>{e(rep.get('diary', ''))}</pre><b>Monthly budget</b><ul>{budget}</ul>
<b>Letter from this future</b><pre>{e(rep.get('letter', ''))}</pre>
{('<b>🎧 Voicemail (' + e(rep.get('mood', '')) + ')</b><pre>' + e(rep['voicemail']) + '</pre>') if rep.get('voicemail') else ''}</div>""")
    body = (f"<p><a href='{_link(tenant, '/')}'>← timeline</a></p><h1>{e(life['question'])}</h1>"
            f"<p class=sub>{e(life['status'])} · {_local(life['created_at'])}</p><div class=tl>{''.join(cols)}</div>"
            f"<h2>Verdict</h2><div class=card><pre>{e(life['summary'] or 'still living…')}</pre></div>")
    return _page("Parallel lives", body)
