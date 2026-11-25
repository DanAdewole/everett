"""Runs INSIDE a timeline Sprite (cc-life-*). Stdlib only: the fresh Sprite has nothing installed.

It reads ~/life/brief.json, "lives" one option of the user's decision a year into the future,
writes the artifacts of that life to its own disk, mails the user a letter from that future,
and leaves ~/life/report.json for the main agent to collect.
"""
import json
import os
import sys
import time
import urllib.error
import urllib.request

HOME = os.path.expanduser("~")
LIFE = os.path.join(HOME, "life")
MODELS = ["gemini-3.5-flash", "gemini-flash-lite-latest", "gemini-3.8-flash", "gemini-flash-latest"]


def http(method, url, body=None, headers=None, timeout=120):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"Content-Type": "application/json", **(headers or {})})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode() or "{}")


def gateways():
    conns = http("GET", "https://api.sprites.dev/v1/gateway/list")["connections"]
    return {c["provider"]: c["gateway_base_url"] for c in conns}


SCHEMA = {
    "type": "object",
    "properties": {
        "headline": {"type": "string", "description": "One-line summary of this life, a year in"},
        "diary": {"type": "string", "description": "A vivid first-person diary entry from exactly one year later, 180-260 words"},
        "monthly_budget": {"type": "array", "items": {"type": "object", "properties": {
            "item": {"type": "string"}, "usd": {"type": "number"}}, "required": ["item", "usd"]}},
        "wins": {"type": "array", "items": {"type": "string"}},
        "hardships": {"type": "array", "items": {"type": "string"}},
        "first_steps": {"type": "array", "items": {"type": "string"}, "description": "3 concrete actions to take this week"},
        "letter": {"type": "string", "description": "A short, honest letter from future-you in this timeline to present-you, 120-180 words"},
        "happiness": {"type": "number", "description": "0-10"},
        "growth": {"type": "number", "description": "0-10"},
        "financial_health": {"type": "number", "description": "0-10"},
    },
    "required": ["headline", "diary", "monthly_budget", "wins", "hardships", "first_steps", "letter",
                 "happiness", "growth", "financial_health"],
}


def live(brief, gw):
    prompt = f"""You are a simulation of ONE possible future of a real person. In this timeline they chose:
>>> {brief['option']} <<<
The decision they faced: {brief['question']}
Other option(s) they did NOT take: {', '.join(brief['others'])}
What we know about them: {brief.get('context') or 'not much: infer gently, stay realistic'}
Today's date: {brief['today']}.

Live this timeline forward exactly one year. Be concrete and realistic (real-world costs, logistics, trade-offs), and honest:
every path has real downsides. Write in first person as the future self."""
    body = {"contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": {"responseMimeType": "application/json", "responseSchema": SCHEMA, "temperature": 0.9}}
    last = None
    for attempt in range(5):
        for m in MODELS:
            try:
                data = http("POST", f"{gw['gemini']}/v1beta/models/{m}:generateContent", body, timeout=150)
                parts = data["candidates"][0]["content"]["parts"]
                text = "".join(p.get("text", "") for p in parts if not p.get("thought"))
                out = json.loads(text)
                out["model"] = m
                return out
            except urllib.error.HTTPError as e:
                last = f"{m}: {e.code} {e.read()[:200]!r}"
            except Exception as e:
                last = f"{m}: {e}"
        time.sleep(min(30, 4 * 2 ** attempt))
    raise RuntimeError(f"gemini failed: {last}")


def main():
    os.makedirs(LIFE, exist_ok=True)
    brief = json.load(open(os.path.join(LIFE, "brief.json")))
    gw = gateways()
    life = live(brief, gw)
    me = os.uname().nodename
    life["sprite"] = me
    life["label"] = brief["label"]
    life["option"] = brief["option"]

    # The artifacts of this life live on this Sprite's own disk.
    with open(os.path.join(LIFE, "diary-one-year-later.md"), "w") as f:
        f.write(f"# Timeline {brief['label']}: {brief['option']}\n\n{life['diary']}\n")
    with open(os.path.join(LIFE, "budget.csv"), "w") as f:
        f.write("item,usd_per_month\n" + "\n".join(f"{b['item']},{b['usd']}" for b in life["monthly_budget"]))
    with open(os.path.join(LIFE, "letter.txt"), "w") as f:
        f.write(life["letter"])

    # Future-you writes to present-you, from inside this timeline.
    if brief.get("recipient") and brief.get("inbox") and "agentmail" in gw:
        total = sum(b["usd"] for b in life["monthly_budget"])
        text = (f"{life['letter']}\n\n"
                f"-- you, one year from now, in Timeline {brief['label']} ({brief['option']})\n"
                f"   Monthly budget here: ${total:,.0f} · Happiness {life['happiness']}/10\n"
                f"   Written from Sprite {me}, a separate computer that only exists in this timeline.")
        try:
            http("POST", f"{gw['agentmail']}/v0/inboxes/{brief['inbox']}/messages/send", {
                "to": [brief["recipient"]],
                "subject": f"📬 A letter from Timeline {brief['label']}: {brief['option']}",
                "text": text})
            life["letter_sent"] = True
        except Exception as e:
            life["letter_error"] = str(e)[:200]

    with open(os.path.join(LIFE, "report.json"), "w") as f:
        json.dump(life, f)
    print("===LIFE_REPORT_BEGIN===")
    print(json.dumps(life))
    print("===LIFE_REPORT_END===")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        os.makedirs(LIFE, exist_ok=True)
        with open(os.path.join(LIFE, "report.json"), "w") as f:
            json.dump({"error": str(e)[:500]}, f)
        print(f"LIFE_ERROR {e}", file=sys.stderr)
        sys.exit(1)
