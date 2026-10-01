"""
Meme Bot v2 - multi-timeframe trend breakouts on meme coins (Alpaca crypto, PAPER ONLY)
======================================================================================

Built on research, adapted to meme coins:
  - Donchian-channel ENSEMBLE breakouts with volatility-based sizing (Zarattini, Pagani & Barbon,
    "Catching Crypto Trends", 2025: Sharpe > 1.5 net of fees on a rotating top-20 coin portfolio).
    Using several lookbacks at once instead of one avoids betting on a single "magic" setting.
  - Time-series momentum has historically beaten cross-sectional momentum in crypto, so each coin
    is judged on its OWN trend.
  - Meme coins are the riskiest corner: the median meme coin is down ~27% this year and spikes
    often fade fast. So the bot (a) refuses to chase coins already up 25%+ in 4 hours,
    (b) needs real volume behind a breakout, and (c) only buys when Bitcoin's trend is healthy,
    because meme coins tend to bleed first when the broader market turns risk-off.

UNIVERSE: DOGE, SHIB, PEPE, BONK, WIF, TRUMP (Alpaca's USD meme pairs). Checked every 15 minutes.

ENTRY (all must be true):
  - breakout score >= 2 of 4: the last completed 15-min bar CLOSED above the prior high of the
    last 12h / 24h / 48h / 96h (brief pokes above that close back inside don't count)
  - last-hour volume >= 1.5x its 24h average
  - 4-hour gain below +25% (not chasing a blow-off top)
  - Bitcoin above its 72-hour average and not down more than 4% in 24h

POSITIONS: up to 2 coins at once, about half the pot each, scaled down for extra-wild coins.

EXITS (whichever comes first):
  - trailing stop: the higher of 10% below the peak or the 12-hour low (rests on Alpaca, 24/7)
  - hard stop 15% below entry
  - trend gone: breakout score falls to 0 AND price drops below its 24h average
  - no follow-through: not up 5% within 12 hours
  After an exit, that coin pauses 6 hours.

Paper only. Not financial advice. Keep crypto_bot.py in the repo (this bot uses its engine).
"""

import os
import statistics
import sys
import traceback
import urllib.request
from datetime import datetime, timedelta, timezone

import crypto_bot as cb
from alpaca.data.historical.crypto import CryptoHistoricalDataClient
from alpaca.data.requests import CryptoBarsRequest, CryptoLatestQuoteRequest
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
from alpaca.trading.client import TradingClient

# ------------------------------ SETTINGS ------------------------------
cb.PAPER = True                                    # hard-locked
cb.BOT_BUDGET = cb.env_float("BOT_BUDGET", 100.0)
cb.STATE_FILE, cb.LOG_FILE = "state_meme.json", "trades_meme.csv"
cb.TAG = "mb2"
cb.COOLDOWN_H = 6
SYMBOLS = [s.strip().upper() for s in (os.getenv("MEME_SYMBOLS") or
           "DOGE/USD,SHIB/USD,PEPE/USD,BONK/USD,WIF/USD,TRUMP/USD").split(",") if s.strip()]
REGIME = "BTC/USD"
WINDOWS_H = (12, 24, 48, 96)       # Donchian ensemble lookbacks, in hours
MIN_SCORE = 2
VOL_SURGE = 1.5
MAX_4H_RUN = 0.25
MAX_POSITIONS = 2
TARGET_DAILY_VOL = 0.08            # coins wilder than ~8%/day get a smaller slice (min half)
TRAIL, HARD_STOP = 0.10, 0.15
TIME_STOP_H, TIME_STOP_GAIN = 12, 0.05
BARS_PER_H = 4                      # 15-minute bars
# ----------------------------------------------------------------------


def notify(force):
    if not cb.NTFY_TOPIC or not cb.messages or not (force or cb.events or cb.NOTIFY_EVERY_RUN):
        return
    try:
        h = {"Title": "Meme bot" + (" (dry run)" if cb.DRY_RUN else "") + (" - ATTENTION" if cb.alerts else "")}
        if cb.alerts:
            h["Priority"] = "high"
        urllib.request.urlopen(urllib.request.Request(f"https://ntfy.sh/{cb.NTFY_TOPIC}",
                                                      data="\n".join(cb.messages[-25:]).encode(), headers=h), timeout=10)
    except Exception as e:
        print(f"(notification failed: {e})")


def until(s, sym):
    cd = s.get("cooldown", {}).get(sym)
    return datetime.fromisoformat(cd) if cd else None


def set_cooldown(s, sym):
    s.setdefault("cooldown", {})[sym] = (datetime.now(timezone.utc) + timedelta(hours=cb.COOLDOWN_H)).isoformat()


def analyze(b, price):
    """Breakout score, volume surge, 4h run, daily vol, 24h average and 12h low for one coin."""
    highs = [x.high for x in b]
    lows = [x.low for x in b]
    closes = [x.close for x in b]
    score = 0
    for w in WINDOWS_H:
        n = w * BARS_PER_H
        if len(b) > n + 1:
            level = max(highs[-n - 2:-2])          # prior high, excluding the last completed bar
            if closes[-1] > level and price > level:  # bar must CLOSE above, not just poke through
                score += 1
    vols = [x.volume or 0 for x in b]
    day = 24 * BARS_PER_H
    avg_vol = (sum(vols[-day - 1:-1]) / day) or 1e-12
    surge = (sum(vols[-BARS_PER_H:]) / BARS_PER_H) / avg_vol
    run4h = price / closes[-4 * BARS_PER_H - 1] - 1
    rets = [closes[i] / closes[i - 1] - 1 for i in range(len(closes) - day, len(closes))]
    daily_vol = statistics.pstdev(rets) * (day ** 0.5) if len(rets) > 2 else 0.1
    return dict(score=score, surge=surge, run4h=run4h, daily_vol=max(daily_vol, 0.01),
                avg24=sum(closes[-day:]) / day, low12=min(lows[-12 * BARS_PER_H:]))


def run(trading, cdata):
    s = cb.load_state()
    try:
        _run(trading, cdata, s)
    finally:
        cb.save_state(s)


def _run(trading, cdata, s):
    now_utc = datetime.now(timezone.utc)
    cb.say(f"PAPER | {'DRY RUN' if cb.DRY_RUN else 'LIVE ORDERS'} | meme trend breakouts | "
           f"{datetime.now(cb.ET):%a %b %d %I:%M %p} ET")
    for k in ("orders", "attempts", "stops", "pos"):
        s.setdefault(k, {})
    cb.settle_orders(trading, s)
    cb.settle_stops(trading, s)

    syms = SYMBOLS + [REGIME]
    bars = cdata.get_crypto_bars(CryptoBarsRequest(symbol_or_symbols=syms, timeframe=TimeFrame(15, TimeFrameUnit.Minute),
                                                   start=now_utc - timedelta(days=6)))
    quotes = cdata.get_crypto_latest_quote(CryptoLatestQuoteRequest(symbol_or_symbols=syms))
    info, marks, stats = {}, {}, {}
    for sym in SYMBOLS:
        try:
            a = trading.get_asset(sym)
            info[sym] = {"pinc": float(a.price_increment or 0), "qinc": float(a.min_trade_increment or 0)}
        except Exception:
            info[sym] = {}
        q = quotes.get(sym)
        b = [x for x in bars.data.get(sym, []) if x.close]
        if q and q.bid_price and q.ask_price:
            marks[sym] = (q.bid_price + q.ask_price) / 2
            if len(b) > max(WINDOWS_H) * BARS_PER_H + 1:
                stats[sym] = analyze(b, marks[sym])

    # ---- Bitcoin regime ----
    bb = [x.close for x in bars.data.get(REGIME, []) if x.close]
    regime_ok, regime_note = True, "BTC data missing"
    if len(bb) > 72 * BARS_PER_H:
        btc = bb[-1]
        ema = cb.ema(bb[-72 * BARS_PER_H * 2:], 72 * BARS_PER_H)
        btc24 = btc / bb[-24 * BARS_PER_H - 1] - 1
        regime_ok = btc > ema and btc24 > -0.04
        regime_note = f"BTC {btc24:+.1%} 24h, {'above' if btc > ema else 'below'} 72h avg"

    # ---- Manage open positions ----
    for sym, pos in list(s["pos"].items()):
        if sym in s["orders"] or sym not in marks:
            continue
        p, q, st = marks[sym], quotes[sym], stats.get(sym)
        pos["peak"] = max(pos.get("peak", p), p)
        opened = datetime.fromisoformat(pos.setdefault("opened", now_utc.isoformat()))
        age_h = (now_utc - opened).total_seconds() / 3600
        chg = p / pos["entry"] - 1
        level = max(pos["peak"] * (1 - TRAIL), pos["entry"] * (1 - HARD_STOP), (st or {}).get("low12", 0))
        why = None
        if p <= level:
            why = f"stop hit ({chg:+.1%})"
        elif st and st["score"] == 0 and p < st["avg24"]:
            why = f"trend gone ({chg:+.1%})"
        elif age_h >= TIME_STOP_H and (pos["peak"] / pos["entry"] - 1) < TIME_STOP_GAIN:
            why = f"no follow-through after {age_h:.0f}h ({chg:+.1%})"
        if why:
            cb.place(trading, s, sym, "sell", pos["qty"], q, True, info[sym])
            cb.say(f"   reason: {why}")
            set_cooldown(s, sym)
        else:
            cb.say(f"{sym}: holding {chg:+.1%} (peak {pos['peak'] / pos['entry'] - 1:+.1%}), stop {cb.px(level)}")
            cb.ensure_stop(trading, s, sym, level, info[sym])

    # ---- New entries ----
    pot = s["cash"] + sum(v["qty"] * marks.get(k, v["entry"]) for k, v in s["pos"].items())
    free = s["cash"] - sum(o.get("notional", 0) for o in s["orders"].values() if o.get("side") == "buy")
    open_count = len(s["pos"]) + sum(1 for o in s["orders"].values() if o.get("side") == "buy")
    if pot <= cb.BOT_BUDGET * 0.3:
        cb.alert(f"Pot is down to ${pot:.2f}. No new entries (kill switch).")
    elif not regime_ok:
        cb.say(f"Market regime off ({regime_note}). Not buying meme coins right now.")
    elif open_count < MAX_POSITIONS:
        candidates = []
        for sym, st in stats.items():
            if sym in s["pos"] or sym in s["orders"]:
                continue
            cd = until(s, sym)
            line = (f"{sym}: score {st['score']}/4, vol {st['surge']:.1f}x, 4h {st['run4h']:+.1%}")
            if cd and cd > now_utc:
                cb.say(line + " (cooling down)")
            elif st["score"] >= MIN_SCORE and st["surge"] >= VOL_SURGE and 0 < st["run4h"] < MAX_4H_RUN:
                candidates.append((st["score"], st["run4h"] / st["daily_vol"], sym, line, st))
            elif st["run4h"] >= MAX_4H_RUN:
                cb.say(line + " (too extended, not chasing)")
            else:
                cb.say(line)
        candidates.sort(reverse=True)
        for score, strength, sym, line, st in candidates[:MAX_POSITIONS - open_count]:
            scale = max(0.5, min(1.0, TARGET_DAILY_VOL / st["daily_vol"]))
            amount = min(pot / MAX_POSITIONS * scale, free) * (1 - cb.FEE_EST)
            if amount < cb.MIN_ORDER:
                cb.say(f"{sym}: signal, but only ${max(free, 0):.2f} free.")
                break
            q = quotes[sym]
            cb.say(f"🚀 BREAKOUT {line} (size {scale:.0%} of a slot)")
            cb.place(trading, s, sym, "buy", amount / q.ask_price, q, True, info[sym])
            if sym in s["pos"]:
                s["pos"][sym]["opened"] = now_utc.isoformat()
            free -= amount / (1 - cb.FEE_EST)
        if not candidates:
            cb.say(f"No qualifying breakout. ({regime_note})")

    pot = s["cash"] + sum(v["qty"] * marks.get(k, v["entry"]) for k, v in s["pos"].items())
    w, l = s.get("wins", 0), s.get("losses", 0)
    cb.say(f"Pot ${pot:.2f} ({pot / cb.BOT_BUDGET - 1:+.1%}) | fees ≈ ${s.get('fees', 0):.2f} | "
           f"trades {w + l}, wins {w}")


def main():
    k, sec = os.getenv("ALPACA_API_KEY"), os.getenv("ALPACA_SECRET_KEY")
    if not k or not sec:
        sys.exit("Missing ALPACA_API_KEY / ALPACA_SECRET_KEY.")
    run(TradingClient(k, sec, paper=True), CryptoHistoricalDataClient(k, sec))


if __name__ == "__main__":
    daily = False
    try:
        main()
        st = cb.load_state()
        today = datetime.now(cb.ET).strftime("%Y-%m-%d")
        if datetime.now(cb.ET).hour >= 20 and st.get("last_daily") != today:
            daily = True
            st["last_daily"] = today
            cb.save_state(st)
    except SystemExit:
        raise
    except Exception as e:
        cb.alert(f"BOT ERROR: {e}")
        traceback.print_exc()
        raise
    finally:
        notify(daily)
