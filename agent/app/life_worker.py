"""Runs INSIDE a timeline Sprite (cc-life-*). Stdlib only: the fresh Sprite has nothing installed.

It reads ~/life/brief.json, "lives" one option of the user's decision a year into the future,
writes the artifacts of that life to its own disk, mails the user a letter from that future,
and leaves ~/life/report.json for the main agent to collect.
"""
import base64
import io
import json
import os
import wave
import sys
import time
import urllib.error
import urllib.request

HOME = os.path.expanduser("~")
LIFE = os.path.join(HOME, "life")
MODELS = ["gemini-3.5-flash", "gemini-flash-lite-latest", "gemini-3.8-flash", "gemini-flash-latest"]
TTS_MODELS = ["gemini-3.8-flash-tts", "gemini-3.8-flash-lite-tts", "gemini-3.1-flash-tts-preview",
              "gemini-2.5-flash-preview-tts"]
VOICES = {"A": "Charon", "B": "Kore", "C": "Puck"}  # each future sounds different
MAX_AUDIO = 4_200_000  # AgentMail caps a whole request at 6 MB, and base64 adds a third


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
        "voicemail": {"type": "string", "description": "A 60-90 word voicemail future-you leaves present-you: spoken, "
                      "warm, specific (one concrete detail from this life), honest about one cost. No stage directions."},
        "mood": {"type": "string", "description": "2-4 words for how future-you sounds, e.g. 'tired but proud'"},
        "happiness": {"type": "number", "description": "0-10"},
        "growth": {"type": "number", "description": "0-10"},
        "financial_health": {"type": "number", "description": "0-10"},
    },
    "required": ["headline", "diary", "monthly_budget", "wins", "hardships", "first_steps", "letter", "voicemail", "mood",
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


def _wav(pcm: bytes, rate: int = 24000) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm)
    return buf.getvalue()


def speak(script: str, mood: str, label: str, gw) -> bytes | None:
    """Future-you, out loud: Gemini text-to-speech through the same connector (free tier)."""
    body = {"contents": [{"parts": [{"text": f"Say this as a voicemail from someone's future self, one year from now. "
                                             f"Sound {mood or 'warm and honest'}, natural, unhurried:\n\n{script}"}]}],
            "generationConfig": {"responseModalities": ["AUDIO"], "speechConfig": {
                "voiceConfig": {"prebuiltVoiceConfig": {"voiceName": VOICES.get(label, "Charon")}}}}}
    for m in TTS_MODELS:
        try:
            data = http("POST", f"{gw['gemini']}/v1beta/models/{m}:generateContent", body, timeout=90)
            part = data["candidates"][0]["content"]["parts"][0]["inlineData"]
            audio = base64.b64decode(part["data"])
            mime = part.get("mimeType", "").lower()
            if "wav" not in mime:  # raw 16-bit PCM ("audio/L16;rate=24000"): wrap it
                rate = int(mime.split("rate=")[1].split(";")[0]) if "rate=" in mime else 24000
                audio = _wav(audio, rate)
            return audio if len(audio) <= MAX_AUDIO else None
        except Exception:
            continue
    return None


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
    audio = speak(life.get("voicemail", ""), life.get("mood", ""), brief["label"], gw) if life.get("voicemail") else None
    if audio:
        with open(os.path.join(LIFE, "voicemail.wav"), "wb") as f:
            f.write(audio)
    life["voicemail_audio"] = bool(audio)

    # Future-you writes to present-you, from inside this timeline.
    if brief.get("recipient") and brief.get("inbox") and "agentmail" in gw:
        total = sum(b["usd"] for b in life["monthly_budget"])
        text = (f"{life['letter']}\n\n"
                f"-- you, one year from now, in Timeline {brief['label']} ({brief['option']})\n"
                f"   Monthly budget here: ${total:,.0f} · Happiness {life['happiness']}/10\n"
                f"   Written from Sprite {me}, a separate computer that only exists in this timeline.")
        msg = {"to": [brief["recipient"]], "subject": f"📬 A letter from Timeline {brief['label']}: {brief['option']}",
               "text": text}
        if audio:
            msg["text"] = "🎧 Future-you left a voicemail too. It's attached.\n\n" + text
            msg["attachments"] = [{"filename": f"voicemail-from-timeline-{brief['label']}.wav",
                                   "content_type": "audio/wav", "content": base64.b64encode(audio).decode()}]
        url = f"{gw['agentmail']}/v0/inboxes/{brief['inbox']}/messages/send"
        try:
            http("POST", url, msg)
            life["letter_sent"] = True
        except Exception as e:
            life["letter_error"] = str(e)[:200]
            if audio:  # never lose the letter over the attachment
                try:
                    msg.pop("attachments")
                    msg["text"] = text
                    http("POST", url, msg)
                    life["letter_sent"] = True
                except Exception as e2:
                    life["letter_error"] = str(e2)[:200]

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
