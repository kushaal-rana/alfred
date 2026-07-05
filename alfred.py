#!/usr/bin/env python3
"""Alfred — Telegram relay to Claude Code running against the Cowork OS workspace.

Long-polls Telegram, forwards messages from the allowed user to a headless
`claude -p` session in the workspace, and sends the reply back. Stdlib only.
"""

import json
import os
import subprocess
import sys
import time
import urllib.parse
import urllib.request

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")
STATE_PATH = os.path.join(BASE_DIR, "state.json")

TELEGRAM_MAX = 3900  # chunk size, safely under Telegram's 4096 limit

SYSTEM_GUARD = (
    "You are Alfred, Kushaal's Cowork assistant reached via Telegram on his phone. "
    "Replies render as plain text in Telegram: no markdown tables, no headers; keep "
    "them tight and phone-readable, use simple dashes for lists. "
    "Read-only questions: just answer. For anything that WRITES (editing workspace "
    "files, sending emails, creating events, changing Notion): state exactly what you "
    "intend to change and ask for a yes first; only act after Kushaal confirms in a "
    "follow-up message. Never touch files outside the workspace."
)


def log(msg):
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}", flush=True)


def load_json(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def save_json(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, path)


class Telegram:
    def __init__(self, token):
        self.base = f"https://api.telegram.org/bot{token}"
        self.file_base = f"https://api.telegram.org/file/bot{token}"

    def call(self, method, http_timeout=65, **params):
        data = urllib.parse.urlencode(params).encode()
        req = urllib.request.Request(f"{self.base}/{method}", data=data)
        with urllib.request.urlopen(req, timeout=http_timeout) as resp:
            return json.load(resp)

    def get_updates(self, offset):
        return self.call("getUpdates", offset=offset, timeout=50,
                         allowed_updates='["message"]')

    def send(self, chat_id, text):
        for i in range(0, len(text), TELEGRAM_MAX):
            chunk = text[i:i + TELEGRAM_MAX]
            try:
                self.call("sendMessage", chat_id=chat_id, text=chunk)
            except Exception as e:
                log(f"sendMessage failed: {e}")

    def typing(self, chat_id):
        try:
            self.call("sendChatAction", chat_id=chat_id, action="typing", http_timeout=10)
        except Exception:
            pass

    def download_file(self, file_id, dest_dir):
        """Fetch a Telegram file into dest_dir; returns the local path or None."""
        try:
            info = self.call("getFile", file_id=file_id)
            remote = info["result"]["file_path"]
            ext = os.path.splitext(remote)[1] or ".jpg"
            name = time.strftime("%Y%m%d-%H%M%S") + "-" + file_id[-8:] + ext
            os.makedirs(dest_dir, exist_ok=True)
            dest = os.path.join(dest_dir, name)
            with urllib.request.urlopen(f"{self.file_base}/{remote}", timeout=60) as resp, \
                    open(dest, "wb") as f:
                f.write(resp.read())
            return dest
        except Exception as e:
            log(f"file download failed: {e}")
            return None


def clean_env(cfg):
    """Env for the claude subprocess: sane PATH, no nested-session markers,
    subscription auth via long-lived OAuth token."""
    env = {k: v for k, v in os.environ.items()
           if not k.startswith("CLAUDE") and k != "CLAUDECODE"}
    extra = "/opt/homebrew/bin:/usr/local/bin:" + os.path.expanduser("~/.local/bin")
    env["PATH"] = extra + ":" + env.get("PATH", "/usr/bin:/bin")
    if cfg.get("claude_oauth_token"):
        env["CLAUDE_CODE_OAUTH_TOKEN"] = cfg["claude_oauth_token"]
    return env


def run_claude(cfg, state, text):
    """Run one headless Claude Code turn; returns (reply, new_session_id)."""
    cmd = [
        cfg["claude_path"], "-p", text,
        "--output-format", "json",
        "--permission-mode", "acceptEdits",
        "--append-system-prompt", SYSTEM_GUARD,
    ]
    if state.get("session_id"):
        cmd += ["--resume", state["session_id"]]

    try:
        proc = subprocess.run(
            cmd, cwd=cfg["workdir"], env=clean_env(cfg),
            capture_output=True, text=True, timeout=cfg.get("turn_timeout", 900),
        )
    except subprocess.TimeoutExpired:
        return "Alfred: that took too long and was stopped. Try again or send /new.", state.get("session_id")

    out = proc.stdout.strip()
    if proc.returncode != 0:
        # A stale/invalid session id is the most common failure — retry fresh once.
        if state.get("session_id"):
            log(f"claude failed (rc={proc.returncode}), retrying without session. stderr: {proc.stderr[:500]}")
            state["session_id"] = None
            return run_claude(cfg, state, text)
        err = (proc.stderr or out or "unknown error")[:800]
        return f"Alfred: Claude errored:\n{err}", None

    try:
        payload = json.loads(out)
        return payload.get("result", "(empty reply)"), payload.get("session_id")
    except json.JSONDecodeError:
        return out or "(empty reply)", state.get("session_id")


def handle(cfg, state, tg, msg):
    chat_id = msg["chat"]["id"]
    user_id = msg.get("from", {}).get("id")
    text = (msg.get("text") or msg.get("caption") or "").strip()

    if user_id not in cfg["allowed_user_ids"]:
        log(f"REJECTED message from unauthorized user {user_id}")
        return

    photos = msg.get("photo") or []
    if photos:
        largest = max(photos, key=lambda p: p.get("file_size") or 0)
        inbox = os.path.join(cfg["workdir"], ".telegram-inbox")
        path = tg.download_file(largest["file_id"], inbox)
        if not path:
            tg.send(chat_id, "Alfred: couldn't download that photo. Try sending it again.")
            return
        text = (f'[Kushaal sent a photo via Telegram, saved at "{path}". '
                f"Read/view the image file before responding.]\n{text}").strip()

    if not text:
        tg.send(chat_id, "Alfred: I only handle text and photo messages for now.")
        return

    if text.lower() in ("/new", "/reset"):
        state["session_id"] = None
        save_json(STATE_PATH, state)
        tg.send(chat_id, "Fresh session started. What do you need?")
        return

    if text.lower() == "/status":
        sid = state.get("session_id") or "none (fresh)"
        tg.send(chat_id, f"Alfred is up.\nWorkspace: {cfg['workdir']}\nSession: {sid}")
        return

    tg.typing(chat_id)
    log(f"-> claude: {text[:120]}")
    reply, session_id = run_claude(cfg, state, text)
    state["session_id"] = session_id
    save_json(STATE_PATH, state)
    log(f"<- reply ({len(reply)} chars)")
    tg.send(chat_id, reply)


def main():
    cfg = load_json(CONFIG_PATH, None)
    if not cfg:
        sys.exit(f"Missing or invalid config at {CONFIG_PATH}")
    state = load_json(STATE_PATH, {"offset": 0, "session_id": None})
    tg = Telegram(cfg["token"])
    log("Alfred relay started.")

    while True:
        try:
            resp = tg.get_updates(state["offset"])
        except Exception as e:
            log(f"getUpdates error: {e}; retrying in 5s")
            time.sleep(5)
            continue

        for upd in resp.get("result", []):
            state["offset"] = upd["update_id"] + 1
            save_json(STATE_PATH, state)
            if "message" in upd:
                try:
                    handle(cfg, state, tg, upd["message"])
                except Exception as e:
                    log(f"handler error: {e}")


if __name__ == "__main__":
    main()
