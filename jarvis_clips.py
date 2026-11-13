"""
Jarvis Clip-keuring — clips goedkeuren in Telegram en automatisch posten op Instagram.

Zo werkt het (draait elke ~5 min via GitHub Actions):
 1. Jij (of later ClipBot) stuurt een clip als VIDEO naar je Jarvis-bot in Telegram.
    De tekst die je bij de video zet, wordt de Instagram-caption.
 2. Jarvis antwoordt met knoppen:  ✅ Posten   ❌ Weg
    Andere caption? Antwoord (reply) op dat bericht met de nieuwe tekst.
 3. Bij ✅ zet Jarvis de clip online (GitHub Pages) en post hem als Reel op Instagram.
    Je krijgt de link terug. Daarna haalt hij de clip weer van GitHub.

Zonder Instagram-koppeling (IG_USER_ID / IG_TOKEN) werkt de keuring ook al:
goedgekeurde clips krijgen dan de status "klaar om te posten".

Secrets in GitHub: TELEGRAM_TOKEN, TELEGRAM_CHAT_ID, (optioneel) IG_USER_ID, IG_TOKEN.
"""

import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
STATE = ROOT / "clips_state.json"
PAGES_DIR = ROOT / "docs" / "c"           # GitHub Pages serveert de map docs/
UA = {"User-Agent": "jarvis-clips/1.0"}
IG_API = "https://graph.instagram.com/v23.0"
DEFAULT_TAGS = os.getenv("JARVIS_TAGS", "#gta6 #gta #gaming #clips")

TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()
IG_USER = os.getenv("IG_USER_ID", "").strip()
IG_TOKEN = os.getenv("IG_TOKEN", "").strip()
REPO = os.getenv("GITHUB_REPOSITORY", "")  # bv. Poelmaster/JarvisTry


# ----------------------------------------------------------------------------
# HTTP-hulpjes
# ----------------------------------------------------------------------------
def http(url, data=None, method=None, timeout=30):
    body = urllib.parse.urlencode(data).encode() if data is not None else None
    req = urllib.request.Request(url, data=body, headers=UA, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        txt = e.read().decode(errors="replace")
        try:
            return json.loads(txt)
        except Exception:  # noqa: BLE001
            return {"ok": False, "error": {"message": f"HTTP {e.code}: {txt[:300]}"}}


def tg(method, _timeout=30, **params):
    for k, v in list(params.items()):
        if isinstance(v, (dict, list)):
            params[k] = json.dumps(v)
    return http(f"https://api.telegram.org/bot{TOKEN}/{method}", params, timeout=_timeout)


def say(text, reply_to=None, buttons=None):
    p = {"chat_id": CHAT_ID, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True}
    if reply_to:
        p["reply_to_message_id"] = reply_to
    if buttons:
        p["reply_markup"] = {"inline_keyboard": buttons}
    return tg("sendMessage", **p)


def edit(message_id, text, buttons=None):
    p = {"chat_id": CHAT_ID, "message_id": message_id, "text": text, "parse_mode": "HTML",
         "disable_web_page_preview": True}
    p["reply_markup"] = {"inline_keyboard": buttons or []}
    return tg("editMessageText", **p)


def esc(s):
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


# ----------------------------------------------------------------------------
# Status
# ----------------------------------------------------------------------------
def load():
    try:
        return json.loads(STATE.read_text())
    except Exception:  # noqa: BLE001
        return {"offset": 0, "clips": {}}


def save(st):
    # oude afgehandelde clips opruimen (max 200 bewaren)
    items = sorted(st["clips"].items(), key=lambda kv: kv[1].get("t", 0))
    done = [k for k, v in items if v["status"] in ("gepost", "weg", "fout", "herwerk", "klaar")]
    for k in done[:-300]:
        st["clips"].pop(k, None)
    STATE.write_text(json.dumps(st, indent=1, ensure_ascii=False))


def card(c):
    cap = c.get("caption") or "(geen caption)"
    return (f"🎬 <b>Clip #{c['n']}</b>\n\n"
            f"<b>Caption:</b>\n{esc(cap)}\n\n"
            f"<i>Andere caption? Antwoord op dit bericht met de nieuwe tekst.</i>")


BUTTONS = lambda key: [[{"text": "✅ Posten", "callback_data": f"ok:{key}"},
                        {"text": "❌ Weg", "callback_data": f"no:{key}"}],
                       [{"text": "➕ Langer", "callback_data": f"lg:{key}"},
                        {"text": "➖ Korter", "callback_data": f"kt:{key}"}]]
REASONS = [("saai", "😴 Saai"), ("begin", "🐢 Slecht begin"), ("context", "🤷 Geen context"),
           ("lang", "⏳ Te lang"), ("kort", "✂️ Te kort"), ("anders", "🙅 Andere reden")]
REASON_BUTTONS = lambda key: [[{"text": lbl, "callback_data": f"r:{key}:{code}"} for code, lbl in REASONS[i:i + 3]]
                              for i in (0, 3)]


# ----------------------------------------------------------------------------
# 1. Nieuwe berichten en knoppen verwerken
# ----------------------------------------------------------------------------
def handle_updates(st, wait=0):
    """Haalt nieuwe berichten op. Met wait > 0 wacht Telegram tot er iets binnenkomt (meteen reageren)."""
    try:
        res = tg("getUpdates", _timeout=wait + 20, offset=st.get("offset", 0), timeout=wait,
                 allowed_updates=["message", "callback_query"])
    except Exception as e:  # noqa: BLE001
        print("getUpdates fout:", e)
        time.sleep(5)
        return False
    if not res.get("ok"):
        print("getUpdates mislukt:", res)
        time.sleep(5)
        return False
    got = res.get("result", [])
    for u in got:
        st["offset"] = u["update_id"] + 1
        if "callback_query" in u:
            on_button(st, u["callback_query"])
        elif "message" in u:
            on_message(st, u["message"])
    return bool(got)


def on_message(st, m):
    if str(m.get("chat", {}).get("id")) != CHAT_ID:
        return  # enkel jij mag Jarvis aansturen
    vid = m.get("video")
    doc = m.get("document")
    if doc and str(doc.get("mime_type", "")).startswith("video/"):
        vid = doc
    if vid:
        if vid.get("file_size", 0) > 20 * 1024 * 1024:
            say("⚠️ Deze clip is groter dan 20 MB. Telegram laat bots dat niet downloaden. "
                "Stuur hem als gewone video (niet als bestand), dan comprimeert Telegram hem.",
                reply_to=m["message_id"])
            return
        st["n"] = st.get("n", 0) + 1
        key = str(m["message_id"])
        raw = (m.get("caption") or "").strip()
        mcb = re.search(r"#cb:([\w-]+)", raw)
        cap = re.sub(r"\s*#cb:[\w-]+", "", raw).strip()
        c = {"n": st["n"], "file_id": vid["file_id"], "caption": cap,
             "status": "wacht", "t": int(time.time())}
        if mcb:
            c["cb"] = mcb.group(1)  # id van ClipBot, om te leren en opnieuw te knippen
        r = say(card(c), reply_to=m["message_id"], buttons=BUTTONS(key))
        c["card"] = (r.get("result") or {}).get("message_id")
        st["clips"][key] = c
        return
    # caption aanpassen: antwoord op de kaart
    rt = m.get("reply_to_message") or {}
    txt = (m.get("text") or "").strip()
    if rt and txt:
        for key, c in st["clips"].items():
            if c.get("card") == rt.get("message_id") and c["status"] == "wacht":
                c["caption"] = txt
                edit(c["card"], card(c), BUTTONS(key))
                say("✏️ Caption aangepast.", reply_to=m["message_id"])
                return
    if txt.lower() in ("/clips", "clips", "status"):
        wacht = [c for c in st["clips"].values() if c["status"] == "wacht"]
        say(f"📋 {len(wacht)} clip(s) wachten op jouw keuring." if wacht else "📋 Geen clips in de wachtrij.")
    elif txt.lower() in ("/start", "/help", "help"):
        say("🤖 <b>Jarvis clip-keuring</b>\nStuur me een clip als video, met de caption als tekst erbij. "
            "Ik vraag dan ✅ of ❌. Bij ✅ post ik hem op Instagram.")


def on_button(st, q):
    if str(q.get("message", {}).get("chat", {}).get("id")) != CHAT_ID:
        return
    data = q.get("data", "")
    act, _, rest = data.partition(":")
    key, _, code = rest.partition(":")
    c = st["clips"].get(key)
    try:
        tg("answerCallbackQuery", callback_query_id=q["id"], text={
            "ok": "Komt in orde", "no": "Weggegooid", "lg": "Wordt langer gemaakt",
            "kt": "Wordt korter gemaakt", "r": "Genoteerd, ik leer ervan"}.get(act, "Ok"))
    except Exception:  # noqa: BLE001
        pass
    if c and act == "r":  # reden bij afkeuren
        c["reden"] = code
        label = dict(REASONS).get(code, code)
        edit(c["card"], f"🗑️ Clip #{c['n']} weggegooid. Reden: {label}\n<i>ClipBot houdt hier rekening mee.</i>")
        return
    if not c or c["status"] != "wacht":
        return
    if act in ("lg", "kt"):
        c["status"] = "herwerk"
        c["herwerk"] = "langer" if act == "lg" else "korter"
        tip = ("\n\nStart op je pc:  <code>python clipbot.py herwerk</code>" if c.get("cb")
               else "\n\n(Deze clip komt niet van ClipBot, dus knip hem zelf opnieuw.)")
        edit(c["card"], f"🔁 Clip #{c['n']} wordt {c['herwerk']} gemaakt.{tip}")
        return
    if act == "ok":
        c["status"] = "goedgekeurd"
        edit(c["card"], card(c).split("\n\n<i>")[0] + "\n\n⏳ <b>Goedgekeurd</b>, wordt gepost…")
    elif act == "no":
        c["status"] = "weg"
        edit(c["card"], f"🗑️ Clip #{c['n']} weggegooid. Waarom? (helpt ClipBot leren)", REASON_BUTTONS(key))


# ----------------------------------------------------------------------------
# 2. Goedgekeurde clips posten
# ----------------------------------------------------------------------------
def git(*args):
    return subprocess.run(["git", *args], cwd=ROOT, check=True, capture_output=True, text=True)


def git_push(msg):
    git("add", "-A")
    if subprocess.run(["git", "diff", "--cached", "--quiet"], cwd=ROOT).returncode == 0:
        return
    git("commit", "-m", msg)
    for _ in range(3):
        try:
            git("push")
            return
        except subprocess.CalledProcessError:
            git("pull", "--rebase")
    raise RuntimeError("git push mislukt")


def pages_url(name):
    owner, _, repo = REPO.partition("/")
    return f"https://{owner.lower()}.github.io/{repo}/c/{name}"


def wait_online(url, minutes=6):
    end = time.time() + minutes * 60
    while time.time() < end:
        try:
            req = urllib.request.Request(url, headers=UA, method="HEAD")
            with urllib.request.urlopen(req, timeout=20) as r:
                if r.status == 200:
                    return True
        except Exception:  # noqa: BLE001
            pass
        time.sleep(15)
    return False


def ig_post(video_url, caption):
    r = http(f"{IG_API}/{IG_USER}/media", {
        "media_type": "REELS", "video_url": video_url, "caption": caption,
        "share_to_feed": "true", "access_token": IG_TOKEN})
    cid = r.get("id")
    if not cid:
        raise RuntimeError(f"Instagram weigerde de clip: {(r.get('error') or {}).get('message', r)}")
    for _ in range(20):  # max ± 5 min wachten tot Instagram de video verwerkt heeft
        time.sleep(15)
        s = http(f"{IG_API}/{cid}?" + urllib.parse.urlencode(
            {"fields": "status_code,status", "access_token": IG_TOKEN}))
        code = s.get("status_code")
        if code == "FINISHED":
            break
        if code in ("ERROR", "EXPIRED"):
            raise RuntimeError(f"Instagram kon de video niet verwerken: {s.get('status', code)}")
    else:
        raise RuntimeError("Instagram had te lang nodig om de video te verwerken.")
    p = http(f"{IG_API}/{IG_USER}/media_publish", {"creation_id": cid, "access_token": IG_TOKEN})
    mid = p.get("id")
    if not mid:
        raise RuntimeError(f"Publiceren mislukt: {(p.get('error') or {}).get('message', p)}")
    link = http(f"{IG_API}/{mid}?" + urllib.parse.urlencode(
        {"fields": "permalink", "access_token": IG_TOKEN})).get("permalink", "")
    return link


def full_caption(c):
    cap = c.get("caption") or ""
    if DEFAULT_TAGS and "#" not in cap:
        cap = (cap + "\n\n" + DEFAULT_TAGS).strip()
    return cap[:2150]


def post_approved(st):
    todo = [(k, c) for k, c in st["clips"].items() if c["status"] == "goedgekeurd"]
    if not todo:
        return
    if not (IG_USER and IG_TOKEN):
        for k, c in todo:
            c["status"] = "klaar"
            edit(c["card"], f"✅ Clip #{c['n']} goedgekeurd.\n\n"
                 "Instagram is nog niet gekoppeld, dus post hem zelf. "
                 "Zodra IG_USER_ID en IG_TOKEN in GitHub staan, doe ik dit automatisch.")
        return
    PAGES_DIR.mkdir(parents=True, exist_ok=True)
    (ROOT / "docs" / ".nojekyll").touch()
    # 1. video's downloaden en online zetten
    ready = []
    for k, c in todo:
        f = tg("getFile", file_id=c["file_id"])
        path = (f.get("result") or {}).get("file_path")
        if not path:
            c["status"] = "fout"
            edit(c["card"], f"⚠️ Clip #{c['n']}: kon de video niet ophalen bij Telegram.")
            continue
        name = f"clip_{c['n']}_{k}.mp4"
        urllib.request.urlretrieve(f"https://api.telegram.org/file/bot{TOKEN}/{path}", PAGES_DIR / name)
        c["file"] = name
        ready.append((k, c))
    if not ready:
        return
    save(st)
    git_push(f"Clips online voor Instagram ({len(ready)})")
    # 2. posten
    for k, c in ready:
        url = pages_url(c["file"])
        try:
            if not wait_online(url):
                raise RuntimeError("de video kwam niet online op GitHub Pages (staat Pages aan?)")
            link = ig_post(url, full_caption(c))
            c["status"] = "gepost"
            c["link"] = link
            edit(c["card"], f"📸 Clip #{c['n']} staat op Instagram!\n{link}")
        except Exception as e:  # noqa: BLE001
            c["status"] = "fout"
            edit(c["card"], f"⚠️ Clip #{c['n']} niet gepost: {esc(str(e))[:300]}\n\n"
                 "Stuur hem gerust opnieuw, of post hem zelf.")
        finally:
            (PAGES_DIR / c["file"]).unlink(missing_ok=True)
    save(st)
    git_push("Geposte clips opgeruimd")


def main():
    if not (TOKEN and CHAT_ID):
        print("TELEGRAM_TOKEN en TELEGRAM_CHAT_ID moeten als secret in GitHub staan.")
        return 1
    st = load()
    st.setdefault("clips", {})
    loop_min = float(os.getenv("JARVIS_LOOP_MIN", "0") or 0)
    end_at = time.time() + loop_min * 60
    while True:
        left = end_at - time.time()
        changed = handle_updates(st, wait=int(min(50, max(0, left - 5))) if loop_min else 0)
        if changed or not loop_min:
            save(st)
            post_approved(st)
            save(st)
            if os.getenv("GITHUB_ACTIONS"):
                try:
                    git_push("Jarvis clip-status")
                except Exception as e:  # noqa: BLE001
                    print("git push mislukt:", e)
        if time.time() >= end_at - 6:
            break
    wacht = sum(1 for c in st["clips"].values() if c["status"] == "wacht")
    print(f"Klaar. {wacht} clip(s) wachten op keuring.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
