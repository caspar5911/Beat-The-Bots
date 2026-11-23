# Beat the Bot ("Huat or Not?")

A daily stock game for a Telegram group: every US trading day, players call whether a few well-known stocks will
**close above or below their opening price**, and an AI (your own self-hosted model) plays against them.
Just a game: no money, not investment advice.

## The idea

A game about **deciding under uncertainty**, not about guessing. Whether a stock closes above its open is close to a
coin flip, so the skill is knowing how sure to be: confident when you have an edge, cautious or skipping when you
don't. Scores reward honest confidence, and the reports show players their own habits.

## How a day goes (New York time; Singapore is +13h in winter)

| When | What happens |
|---|---|
| 07:30 | Round posted in every group: 5 industry funds (ETFs, e.g. XLF banks, XLE oil) from 5 different industries (up to 3 are the day's biggest pre-market movers), except that the 2 stocks players suggested most with `/suggest` take 2 of the places; each with each with its pre-market move and one headline. Buttons per stock: ⏫ very sure up · ⬆️ think so up · ⬇️ think so down · ⏬ very sure down. The AI makes its calls with an honest confidence, hidden. |
| 09:20 | Reminder tagging regular players who still have picks to make. |
| 09:30 | Picks lock: the group's split per stock and the AI's calls, confidence and reasons are revealed. |
| 11:00 | Halfway update (optional: `/halfway on`): live standings. |
| close + 10 min | Results: the split, who was right against the crowd (🦅), group brain vs the AI, a daily 📚 lesson, ❓ "why did it move" buttons, the AI's banter. On the week's last trading day: titles, league, leaderboard image and a 📊 decision report per player. |
| Sat 10:00 SGT | Weekend quiz (optional: `/quiz on`): 5 Telegram quiz questions on money basics and decision habits (instant explanations). Scores Sunday 20:00 SGT. |

## Scoring

| Pick | Right | Wrong | Pays when you're right more than |
|---|---|---|---|
| ⏫/⏬ Sure | +5 | −11 | 69% |
| ⬆️/⬇️ Lean | +3 | −4 | 57% |
| no tap (skip) | 0 | 0 | |

Tap a pick again to undo it. 🚀 Naming the stock with the biggest open-to-close move (either way) is +2. 🔥 marks
3+ correct calls in a row. Weekly titles: 👑 Bot Slayer and 🤡 the AI's Snack.

## Commands

`/today`, `/board`, `/me`, `/help`, plus:
- `/report`: your decision report: how often your Sure and Lean picks are right, which reasons work, skips
- `/bot`: the AI's record and calibration, its hall of shame and best call
- `/card`: your weekly scorecard as an image, to share
- `/league`, `/league join`, `/league leave`: groups vs groups by average points per player (opt-in: joining shows
  the group's name to the other groups)
- `/practice`: a 2-minute unscored round on the last completed session, playable at any hour
- `/select`: a menu of all industries; tap to add ✅ or remove them from tonight's game (while picks are open)
  or the next game. `/set chips banks NVDA` does the same by typing. Main group only.
- `/restart` (or 🔄 in the `/select` menu): restart today's game before picks close; asks first, deletes that
  game's picks, posts a new one (using any `/select`/`/set` choice) and the AI picks again. Main group only.
- `/add NVDA`: add a stock to **today's** game until picks close (up to 3 a day). It joins the game message with
  its own buttons; picking it is optional and reminders ignore it. After the close it becomes a suggestion.
- `/suggest NVDA`: suggest a stock for the next game; the 2 most-suggested replace 2 of its funds (one
  suggestion per player per game; a new one replaces the old)
- `/pick NVDA`: an **own pick** on any US stock, any time. During the session it starts from the current price
  (until 30 minutes before the close); otherwise from the next open. It's judged at that session's close with the
  same points, on a separate "own picks" score. Up to 3 a day per player; anyone can tap the buttons to pick the same
  stock themselves.

**Any group can play**: add the bot to a group and it joins (up to `BTB_MAX_GROUPS`, default 20). All groups get
the same daily stocks and AI calls; each has its own post, reminders and leaderboard, and a player's picks count
in every group they play in.

The round still runs if the model is down (the AI sits out; lessons and explanations fall back to templates), and a
missed stage is caught up after a restart. A stock whose open or close cannot be confirmed from quotes, or that
closes exactly flat, is not scored. NYSE holidays and half-days are listed for 2026-2027 in `btb/market.py`.
Names are set in `.env` (`BTB_GAME_NAME`, `BTB_BOT_NAME`; defaults "Huat or Not?" and "🤖 Ah Huat").

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
