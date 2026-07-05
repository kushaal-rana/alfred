# Alfred — Telegram relay to Claude Code

A tiny always-on bridge that lets a phone talk to a local Claude Code
("Cowork") workspace through a private Telegram bot. Text the bot from
anywhere; it runs a headless `claude -p` turn inside the workspace folder and
replies. Same files as the desktop, so it is bidirectional by definition —
nothing to sync.

## How it works

```
Telegram app (phone)
        │  long-poll (no open ports, no public URL)
        ▼
alfred.py  — relay on the Mac (launchd service, auto-restart)
        │  claude -p "<message>" --resume <session>
        ▼
Claude Code, headless, cwd = the workspace root
(reads CLAUDE.md / MEMORY.md, routes to workstation folders, edits files)
```

- **Persistent session**: follow-ups remember context (`--resume`); auto-recovers
  with a fresh session if the stored one goes stale.
- **Security**: single-user allowlist by Telegram user ID; config file with
  tokens is chmod 600 and gitignored; bot token revocable via @BotFather.
- **Write safety**: system prompt enforces propose-then-confirm for anything
  that writes (files, email, calendar); read-only questions answer immediately.
- **Commands**: `/new` resets the session, `/status` health-checks.

## Setup

1. Create a bot with @BotFather, keep the token.
2. `claude setup-token` for a long-lived subscription OAuth token.
3. `cp config.example.json config.json`, fill it in, `chmod 600 config.json`.
4. Message the bot once, then grab your numeric user ID from
   `https://api.telegram.org/bot<TOKEN>/getUpdates` and put it in
   `allowed_user_ids`.
5. Install the launchd service (edit paths to taste):
   `~/Library/LaunchAgents/com.<you>.alfred.plist` → `launchctl bootstrap gui/$(id -u) <plist>`

Stdlib-only Python; no dependencies.
