"""Weekend quiz: Telegram quiz polls on money basics and decision-making, plus one question on last week's game.

Saturday 10:00 Singapore: 5 questions per group (instant feedback with an explanation from Telegram).
Sunday 20:00 Singapore: the group's quiz scores. Questions rotate by week so a group sees each one once in 6 weeks.
Educational only: facts and thinking habits, never what to buy."""
from __future__ import annotations

import datetime as dt

# (question, options, index of the right option, explanation <= 200 chars)
BANK: list[tuple[str, list[str], int, str]] = [
    ("A stock trades at $50 and earned $5 per share last year. What is its P/E ratio?",
     ["5", "10", "45", "250"], 1, "P/E = price / earnings per share = 50 / 5 = 10. Investors pay $10 for each $1 of yearly profit."),
    ("What is an ETF?", ["A loan from a broker", "A fund that trades on an exchange like a stock",
                         "A type of government bond", "A company's yearly report"], 1,
     "An exchange-traded fund holds a basket of assets (often an index) and you buy or sell it like a single stock."),
    ("A 'bear market' usually means prices have fallen at least…", ["5%", "10%", "20%", "50%"], 2,
     "By convention a fall of 20% or more from a recent high is a bear market; 10% is usually called a correction."),
    ("Why do people diversify?", ["To guarantee profits", "So one bad holding can't sink everything",
                                  "To pay lower taxes", "To trade more often"], 1,
     "Spreading money across many holdings reduces the damage from any single one going wrong. It lowers risk, it doesn't guarantee gains."),
    ("$1,000 grows 10% a year for 2 years, reinvested. How much do you have?", ["$1,100", "$1,200", "$1,210", "$1,300"], 2,
     "Year 1: $1,100. Year 2: 10% of $1,100 = $110 more, so $1,210. Growth on growth is compounding."),
    ("Inflation is 3% and your savings earn 1%. What happens to what your money can buy?",
     ["It rises", "It stays the same", "It falls about 2% a year", "It doubles"], 2,
     "Real return is roughly 1% - 3% = -2%: the balance grows, but prices grow faster."),
    ("Investing the same amount every month regardless of price is called…",
     ["Day trading", "Dollar-cost averaging", "Short selling", "Leverage"], 1,
     "Dollar-cost averaging buys more shares when prices are low and fewer when high, and removes the need to time the market."),
    ("The gap between the highest buy price and lowest sell price is the…", ["Spread", "Dividend", "Yield", "Margin"], 0,
     "The bid-ask spread is a hidden cost of trading: buy and sell instantly and you lose it. Busy stocks have small spreads."),
    ("A limit order to buy at $20 means…", ["Buy now at any price", "Buy only at $20 or lower",
                                             "Buy only above $20", "Buy $20 worth of shares"], 1,
     "A limit order sets the worst price you accept. It may not fill; a market order fills now at whatever price is available."),
    ("A company's market capitalisation is…", ["Its yearly sales", "Share price x number of shares",
                                               "Its cash in the bank", "Its profit after tax"], 1,
     "Market cap = share price x shares outstanding: what the market values the whole company at today."),
    ("After a 2-for-1 stock split, an investor's holding is worth…", ["Twice as much", "Half as much",
                                                                      "The same", "Nothing until resold"], 2,
     "You get twice the shares at half the price each, so the value is unchanged. Splits change the slicing, not the cake."),
    ("An index fund's 'expense ratio' of 0.2% means…", ["It returns 0.2% a year", "0.2% of your money is charged yearly in fees",
                                                         "It holds 0.2% cash", "It can fall 0.2% at most"], 1,
     "Fees come off every year whatever happens. Over decades, small fee differences compound into large sums."),
    ("You think a coin is fair. It landed heads 5 times in a row. The chance of heads next is…",
     ["Lower: tails is due", "Higher: heads is hot", "50%", "Impossible to say"], 2,
     "Independent events have no memory. Expecting a reversal 'because it's due' is the gambler's fallacy."),
    ("You only notice news that agrees with your stock pick. That's called…", ["Confirmation bias", "Diversification",
                                                                               "Hindsight", "Arbitrage"], 0,
     "Confirmation bias: we seek and trust evidence for what we already believe. Good deciders look for the opposing case."),
    ("You lost money on a trade, but your reasons were sound and the risk was sized. Was it a bad decision?",
     ["Yes, it lost money", "Not necessarily: judge the process, not just the result", "Only if others won", "Yes, always"], 1,
     "Judging decisions only by outcomes is 'resulting' (outcome bias). Good decisions can lose and bad ones can win by luck."),
    ("You keep a losing stock only because you already lost a lot on it. That's…", ["Sunk cost fallacy", "Compounding",
                                                                                    "Rebalancing", "Hedging"], 0,
     "Money already lost is gone either way. The only question is whether you'd buy it today at today's price."),
    ("Studying only the funds that still exist today overstates past returns because of…",
     ["Inflation", "Survivorship bias", "Liquidity", "Dividends"], 1,
     "Failed funds close and drop out of the data, so the survivors look better than the average fund really did."),
    ("A friend says they're '90% sure' about picks and is right half the time. They are…", ["Well calibrated", "Overconfident",
                                                                                         "Underconfident", "Diversified"], 1,
     "Calibration means 90% calls come true about 90% of the time. Being right 50% on '90% sure' calls is overconfidence."),
    ("Everyone in the group picks the same side, so you do too. The risk you're running is…",
     ["Herd behaviour", "Currency risk", "Interest rate risk", "Liquidity risk"], 0,
     "Herding feels safe, but crowds are often wrong together. Make your call first, then check what others think."),
    ("A stock gaps up 5% before the open on good news. What do you know about the rest of the day?",
     ["It will keep rising", "It will fall back", "Not much: the news is largely priced in at the open",
      "It will close at its high"], 2,
     "Markets react fast. Once the open reflects the news, the remaining move is close to a coin flip in either direction."),
    ("What is a dividend?", ["A loan to the company", "A share of profits paid to shareholders",
                             "A fee for trading", "A type of bond"], 1,
     "Companies may pay part of their profits to shareholders as dividends. They are not guaranteed and can be cut."),
    ("Short selling a stock means you profit if…", ["It rises", "It falls", "It pays a dividend", "It splits"], 1,
     "Short sellers borrow and sell shares, hoping to buy them back cheaper. Losses are unlimited if the price keeps rising."),
    ("Using borrowed money to invest (leverage) does what to your gains and losses?",
     ["Shrinks both", "Magnifies both", "Magnifies only gains", "Removes losses"], 1,
     "Leverage multiplies results in both directions. A 10% fall with 2x leverage is a 20% loss, plus interest."),
    ("A savings account pays 2% a year. Roughly how long to double your money? (Rule of 72)",
     ["7 years", "18 years", "36 years", "72 years"], 2,
     "Rule of 72: years to double = 72 / interest rate. 72 / 2 = 36 years; at 8% it's about 9."),
    ("What does 'volatility' measure?", ["How profitable a company is", "How much a price swings",
                                         "How many shares trade", "How big a dividend is"], 1,
     "Volatility is the size of price swings. High volatility means bigger moves both up and down, not higher returns."),
    ("You check a stock's price 20 times a day. What does research say tends to happen?",
     ["You make better decisions", "You trade more and feel losses more, often hurting returns",
      "Nothing changes", "The stock moves less"], 1,
     "Frequent checking makes noise feel like signal. Losses hurt about twice as much as gains feel good (loss aversion)."),
    ("A company 'beats earnings' but the stock falls. The most likely reason is…", ["A data error", "Expectations were even higher",
                                                                                  "Dividends were cut", "It's illegal"], 1,
     "Prices move on surprise versus expectations. A beat that's smaller than investors hoped can still disappoint."),
    ("Before a decision, writing down why you made it and what would prove you wrong is called…",
     ["A decision journal", "A balance sheet", "A stop order", "A prospectus"], 0,
     "A decision journal fights hindsight bias: later you can check whether you were right for the right reasons."),
    ("Which is generally riskier for a single investor?", ["A broad index fund", "One small company's stock",
                                                          "A government bond fund", "A savings account"], 1,
     "A single small company can fail outright; a broad index spreads that company-specific risk across hundreds of firms."),
    ("A tip says 'Stock X will double, act now!' What's the healthiest first reaction?",
     ["Buy before others do", "Ask who gains if you act and what the evidence is", "Borrow to buy more", "Tell friends"], 1,
     "Urgency plus certainty is a classic sign of hype or scams. Check the source, their incentive and the evidence first."),
]

PER_WEEK = 4          # bank questions per quiz, plus one on last week's game


def week_key(day: dt.date) -> str:
    """ISO week of the Saturday that starts the quiz weekend."""
    y, w, _ = day.isocalendar()
    return f"{y}-W{w:02d}"


def pick(week: str, n: int = PER_WEEK) -> list[int]:
    """Rotate through the bank: consecutive weeks get consecutive, non-overlapping slices."""
    y, w = week.split("-W")
    start = ((int(y) * 53 + int(w)) * n) % len(BANK)
    return [(start + i) % len(BANK) for i in range(n)]


def game_question(moves: list[tuple[str, str, float]]) -> tuple[str, list[str], int, str] | None:
    """From last week's settled stocks [(day, ticker, move)]: which had the biggest single-day move?"""
    if len(moves) < 4:
        return None
    ranked = sorted(moves, key=lambda m: -abs(m[2]))
    top = ranked[0]
    others = []
    for m in ranked[len(ranked) // 2:] + ranked[1:len(ranked) // 2]:   # plausible but smaller movers
        if m[1] != top[1] and m[1] not in [o[1] for o in others]:
            others.append(m)
        if len(others) == 3:
            break
    if len(others) < 3:
        return None
    options = sorted([top[1]] + [o[1] for o in others])
    day = dt.date.fromisoformat(top[0])
    return ("From last week's game: which stock had the biggest open-to-close move in a single day?", options,
            options.index(top[1]),
            f"{top[1]} moved {top[2]:+.1%} on {day:%a %d %b}. Big moves usually follow big news: watch the headlines.")
