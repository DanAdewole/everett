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
MODELS = ['gemini-3.5-flash', 'gemini-3-flash-preview', 'gemini-3.8-flash', 'gemini-3.7-flash', 'gemini-3.6-flash', 'gemini-flash-latest', 'gemini-3.5-flash-lite', 'gemini-flash-lite-latest', 'gemini-3.1-flash-lite']
TTS_MODELS = ["gemini-3.8-flash-tts", "gemini-3.8-flash-lite-tts", "gemini-3.1-flash-tts-preview",
              "gemini-2.5-flash-preview-tts"]
VOICES = {"A": "Charon", "B": "Kore", "C": "Puck"}  # Gemini voices: each future sounds different
# ElevenLabs premade voices (usable on the free plan): George, Sarah, Brian
ELEVEN_VOICES = {"A": "JBFqnCBsd6RMkjVDRZzb", "B": "EXAVITQu4vr4xnAvoGeH", "C": "nPczCjzI2devNBz1zQrb"}
MAX_AUDIO = 4_200_000  # AgentMail caps a whole request at 6 MB, and base64 adds a third


def http(method, url, body=None, headers=None, timeout=120):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"Content-Type": "application/json", **(headers or {})})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode() or "{}")


def gateways():
    conns = http("GET", "https://api.sprites.dev/v1/gateway/list")["connections"]
    gw = {c["provider"]: c["gateway_base_url"] for c in conns}
    for c in conns:
        # The Fly ElevenLabs connector only covers voice agents; text-to-speech goes through a Custom API
        # connector named "...ElevenLabs..." (base https://api.elevenlabs.io, header xi-api-key).
        label = " ".join(str(c.get(k) or "") for k in ("display_name", "description", "provider_account_name"))
        if c["provider"] == "custom_api" and "elevenlabs" in label.lower():
            gw["elevenlabs_tts"] = c["gateway_base_url"]
    return gw


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
    spent = set()  # models whose daily free quota is used up: don't ask again this run
    for attempt in range(6):
        for m in [x for x in MODELS if x not in spent] or MODELS:
            try:
                data = http("POST", f"{gw['gemini']}/v1beta/models/{m}:generateContent", body, timeout=150)
                parts = data["candidates"][0]["content"]["parts"]
                text = "".join(p.get("text", "") for p in parts if not p.get("thought"))
                out = json.loads(text)
                out["model"] = m
                return out
            except urllib.error.HTTPError as e:
                detail = e.read()[:2000]
                last = f"{m}: {e.code} {detail[:200]!r}"
                if e.code == 429 and b"PerDay" in detail:
                    spent.add(m)
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


def speak_eleven(script: str, label: str, gw) -> bytes | None:
    """ElevenLabs (if its connector is attached): the most human-sounding option. Returns MP3 bytes."""
    if "elevenlabs_tts" not in gw:
        return None
    url = (f"{gw['elevenlabs_tts']}/v1/text-to-speech/{ELEVEN_VOICES.get(label, ELEVEN_VOICES['A'])}"
           f"?output_format=mp3_44100_64")
    req = urllib.request.Request(url, method="POST", data=json.dumps({
        "text": script, "model_id": "eleven_multilingual_v2",
        "voice_settings": {"stability": 0.45, "similarity_boost": 0.8, "style": 0.35}}).encode(),
        headers={"Content-Type": "application/json", "Accept": "audio/mpeg"})
    try:
        with urllib.request.urlopen(req, timeout=90) as r:
            audio = r.read()
        return audio if audio[:3] == b"ID3" or audio[:2] == b"\xff\xfb" or len(audio) > 10_000 else None
    except Exception:
        return None  # out of free credits, blocked endpoint, ...: fall back to Gemini


def speak(script: str, mood: str, label: str, gw) -> tuple[bytes, str] | None:
    """Future-you, out loud. ElevenLabs first, then Gemini text-to-speech (free tier). Returns (audio, extension)."""
    audio = speak_eleven(script, label, gw)
    if audio and len(audio) <= MAX_AUDIO:
        return audio, "mp3"
    wav = speak_gemini(script, mood, label, gw)
    return (wav, "wav") if wav else None


def speak_gemini(script: str, mood: str, label: str, gw) -> bytes | None:
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
    spoken = speak(life.get("voicemail", ""), life.get("mood", ""), brief["label"], gw) if life.get("voicemail") else None
    audio, ext = spoken if spoken else (None, "")
    if audio:
        with open(os.path.join(LIFE, f"voicemail.{ext}"), "wb") as f:
            f.write(audio)
    life["voicemail_audio"] = ext or False

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
            msg["attachments"] = [{"filename": f"voicemail-from-timeline-{brief['label']}.{ext}",
                                   "content_type": "audio/mpeg" if ext == "mp3" else "audio/wav",
                                   "content": base64.b64encode(audio).decode()}]
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
