"""One-time interactive setup: python -m btb setup

Asks for the game bot's token (hidden input), finds your group when you add the bot to it, saves both to .env,
says hello in the group and starts the systemd user service."""
from __future__ import annotations

import getpass
import re
import shutil
import subprocess
import time
from pathlib import Path

import requests

from .config import ROOT


def set_env(path: Path, key: str, value: str) -> None:
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    for i, line in enumerate(lines):
        if line.split("=", 1)[0].strip() == key:
            lines[i] = f"{key}={value}"
            break
    else:
        lines.append(f"{key}={value}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    path.chmod(0o600)


def group_chats(updates: list[dict]) -> dict[int, str]:
    """Groups the bot was added to or messaged in, newest last; follows a group's upgrade to a supergroup."""
    found = {}
    for u in updates:
        for key in ("my_chat_member", "message", "edited_message"):
            item = u.get(key) or {}
            chat = item.get("chat") or {}
            if chat.get("type") in ("group", "supergroup"):
                if item.get("migrate_to_chat_id"):
                    found.pop(chat["id"], None)
                    found[item["migrate_to_chat_id"]] = chat.get("title", "")
                    continue
                if key == "my_chat_member" and (item.get("new_chat_member") or {}).get("status") in ("left", "kicked"):
                    found.pop(chat["id"], None)
                    continue
                found[chat["id"]] = chat.get("title", "")
    return found


def main() -> int:  # pragma: no cover - interactive
    env = ROOT / ".env"
    if not env.exists():
        shutil.copy(ROOT / ".env.example", env)
    print("Beat the Bot setup\n")
    print("1) In Telegram, open @BotFather, send /newbot, pick a name and a username ending in 'bot'.")
    print("   BotFather replies with a token like 123456789:AA...  (never reuse your trading bot's token)\n")
    token = getpass.getpass("Paste the NEW bot's token here (input is hidden): ").strip()
    if not re.fullmatch(r"\d+:[A-Za-z0-9_-]{30,}", token):
        print("That doesn't look like a bot token. Run setup again.")
        return 2
    api = f"https://api.telegram.org/bot{token}"
    me = requests.get(f"{api}/getMe", timeout=20).json()
    if not me.get("ok"):
        print("Telegram rejected that token. Check it in @BotFather and run setup again.")
        return 2
    username = me["result"]["username"]
    set_env(env, "BTB_TELEGRAM_TOKEN", token)
    print(f"   OK: the game bot is @{username}. Token saved to .env (not committed to git).\n")

    print(f"2) Add @{username} to your game group now (open the group > Add members > search {username}).")
    print("   Waiting up to 5 minutes...")
    offset, chats, deadline = 0, {}, time.time() + 300
    while time.time() < deadline and not chats:
        r = requests.get(f"{api}/getUpdates", timeout=40,
                         params={"timeout": 25, "offset": offset,
                                 "allowed_updates": '["message","my_chat_member"]'}).json()
        updates = r.get("result", []) if r.get("ok") else []
        if updates:
            offset = updates[-1]["update_id"] + 1
        chats.update(group_chats(updates))
    if not chats:
        print("   No group seen. Add the bot to a group (or send a message there) and run setup again.")
        return 2
    chat_id, title = list(chats.items())[-1]
    set_env(env, "BTB_CHAT_ID", str(chat_id))
    requests.get(f"{api}/getUpdates", timeout=20, params={"offset": offset, "timeout": 0})  # mark as read
    print(f"   OK: playing in '{title}' ({chat_id}).\n")

    from .config import Settings
    from .game import help_text
    names = Settings.from_env()
    requests.post(f"{api}/sendMessage", timeout=20, json={"chat_id": chat_id, "parse_mode": "HTML",
                                                          "text": help_text(names.game_name, names.bot_name)})

    print("3) Starting the game service...")
    unit_dir = Path.home() / ".config/systemd/user"
    unit_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy(ROOT / "deploy/beat-the-bot.service", unit_dir / "beat-the-bot.service")
    for cmd in (["daemon-reload"], ["enable", "beat-the-bot.service"], ["restart", "beat-the-bot.service"]):
        subprocess.run(["systemctl", "--user", *cmd], check=True)
    time.sleep(3)
    state = subprocess.run(["systemctl", "--user", "is-active", "beat-the-bot.service"],
                           capture_output=True, text=True).stdout.strip()
    print(f"   Service is {state}. Logs: journalctl --user -u beat-the-bot -f")
    print("\nDone! Rounds post every US trading day at 08:45 New York (21:45 Singapore in winter).")
    return 0 if state == "active" else 1
