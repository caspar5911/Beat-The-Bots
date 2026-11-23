# Beat the Bot

A daily stock game for a Telegram group: every US trading day, players call whether a few well-known stocks will
**close above or below their opening price**, and an AI (your own self-hosted model) plays against them.
Just a game: no money, not investment advice.

## How a day goes (New York time)

| When | What happens |
|---|---|
| 08:45 | Round posted: 5 stocks, pre-market move, two fresh headlines each, ⬆️/⬇️ buttons. The Bot makes its calls, hidden. |
| until 09:30 | Players tap their calls (changeable). Votes are judged by the time the server receives them. |
| 09:30 | Picks lock, the Bot's calls and one-line trash talk are revealed. |
| 09:35+ | Opening prices captured. |
| close + 10 min | Results, who beat the Bot, weekly leaderboard. |

Commands in the group: `/today`, `/board`, `/me`, `/help`.

The round still runs if the model is down (the Bot sits out), and a missed stage is caught up after a restart.
A stock whose open or close cannot be confirmed from quotes, or that closes exactly flat, is not scored.
NYSE holidays and half-days are listed for 2026-2027 in `btb/market.py`.

## Quick setup

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python -m btb setup
```

It asks for a new @BotFather token (hidden input), waits for you to add the bot to your group, saves both to
`.env`, says hello in the group and starts the systemd user service. Set `GLM_BASE_URL` in `.env` first.

## Manual setup

Needs Python 3.12+, a running Moomoo OpenD (quote snapshots only; no history quota, no trade context) and an
OpenAI-compatible model endpoint.

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env   # then edit it
```

1. In Telegram, create a **new** bot with @BotFather (never reuse another bot's token: two programs polling one
   token steal each other's messages). Put the token in `.env` as `BTB_TELEGRAM_TOKEN`.
2. Create a group, add the bot, send `/help@YourBotName` in the group (the `@` part makes sure the bot sees it), then run `.venv/bin/python -m btb whereami` and put the
   group's id (a negative number) in `.env` as `BTB_CHAT_ID`.
3. Try a real round without sending anything: `.venv/bin/python -m btb demo`.
4. Run it as a service:

```bash
cp deploy/beat-the-bot.service ~/.config/systemd/user/
systemctl --user daemon-reload && systemctl --user enable --now beat-the-bot
journalctl --user -u beat-the-bot -f
```

## Tests

```bash
.venv/bin/python -m pytest -q
```
