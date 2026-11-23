# Beat the Bot ("Huat or Not?")

A daily stock game for a Telegram group: every US trading day, players call whether a few well-known stocks will
**close above or below their opening price**, and an AI (your own self-hosted model) plays against them.
Just a game: no money, not investment advice.

## How a day goes (New York time)

| When | What happens |
|---|---|
| 07:30 | Round posted in every group: 3 of the day's biggest pre-market movers + 2 random stocks, pre-market move, two fresh headlines each, ⬆️ / ⬇️ / 🎯 x2 buttons and the 🚀 biggest-mover bonus. The AI makes its calls, hidden. |
| 09:20 | Reminder tagging regular players who still have picks to make. |
| until 09:30 | Players tap their calls (changeable). Votes are judged by the time the server receives them. |
| 09:30 | Picks lock; the AI's calls, one-line trash talk and its double-down are revealed. |
| 09:35+ | Opening prices captured. |
| 11:00 | Halftime: live standings from current prices. |
| close + 10 min | Results, streaks, the AI's banter about named players, weekly leaderboard; on the week's last trading day: titles, the league table and a leaderboard image. |

Scoring: 1 point per correct call. **🚀 Bonus**: +2 for naming the stock with the biggest open-to-close move
(either way; not scored if any price is missing). **Double down** (🎯 x2) on one pick a day: +2 if right, −1 if wrong; the AI
doubles its most confident call. 🔥 marks a run of 3+ correct calls. Weekly titles: 👑 Bot Slayer (top human, if
they beat the AI) and 🤡 the AI's Snack (bottom human, 3+ picks).

Commands in the group: `/today`, `/board`, `/me`, `/help`, plus:
- `/bot`: the AI's record, its hall of shame (worst calls) and best call
- `/card`: your weekly scorecard as an image, to share
- `/league`, `/league join`, `/league leave`: groups vs groups by average points per player (opt-in: joining shows
  the group's name to the other groups)
- `/practice`: a 2-minute unscored round on the last completed session, playable at any hour

**Any group can play**: add the bot to a group and it joins (up to `BTB_MAX_GROUPS`, default 20). All groups get
the same daily stocks and AI calls; each has its own post, reminders and leaderboard, and a player's picks count
in every group they play in. Names are set in `.env` (`BTB_GAME_NAME`,
`BTB_BOT_NAME`; defaults "Huat or Not?" and "🤖 Ah Huat").

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
