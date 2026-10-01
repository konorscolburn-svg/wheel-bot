"""
Meme Bot - high-risk meme-coin momentum (Alpaca crypto, PAPER ONLY)
==================================================================

Chases the hottest meme coin when it breaks out, rides it with a trailing stop, and gets out
fast when the move dies. Built on the same engine as crypto_bot.py (keep that file in the repo).

UNIVERSE: meme coins Alpaca lists against USD: DOGE, SHIB, PEPE, BONK, WIF, TRUMP.

ENTRY (checked every 15 minutes, 24/7), all must be true for a coin:
  - up at least 5% over the last 4 hours (momentum)
  - trading above its highest price of the previous 24 hours (a breakout, not just noise)
  - volume in the last hour at least 2x its 24-hour average (real buying, not a thin wiggle)
  If several qualify, it takes the one with the strongest 4-hour move.

POSITION: ALL-IN on one coin at a time (this is the lottery-ticket bot).

EXITS:
  - trailing stop 10% below the highest price since entry (rests on Alpaca, triggers 24/7)
  - hard stop 15% below entry
  - time stop: if it hasn't gained 5% within 12 hours, sell (the pump didn't come)
  - after any exit, that coin is paused for 12 hours

Reality check: meme coins can drop 20-40% in minutes, and stops can fill well below their
price in a crash. Most breakouts fail. Expect many small-to-medium losses and rare big wins.
Not financial advice.
"""

import os
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
cb.PAPER = True                                   # hard-locked: this bot never trades real money
cb.BOT_BUDGET = cb.env_float("BOT_BUDGET", 100.0)
cb.STATE_FILE, cb.LOG_FILE = "state_meme.json", "trades_meme.csv"
cb.TAG = "mb1"
cb.COOLDOWN_H = 12
SYMBOLS = [s.strip().upper() for s in (os.getenv("MEME_SYMBOLS") or
           "DOGE/USD,SHIB/USD,PEPE/USD,BONK/USD,WIF/USD,TRUMP/USD").split(",") if s.strip()]
MOMENTUM_4H = 0.05        # +5% in 4 hours
VOLUME_SURGE = 2.0        # last hour volume vs 24h average
TRAIL = 0.10              # 10% trailing stop
HARD_STOP = 0.15          # 15% hard stop
TIME_STOP_H = 12          # exit if not +5% within 12 hours
TIME_STOP_GAIN = 0.05
# ----------------------------------------------------------------------


def notify(force):
    if not cb.NTFY_TOPIC or not cb.messages or not (force or cb.events or cb.NOTIFY_EVERY_RUN):
        return
    try:
        title = "Meme bot" + (" (dry run)" if cb.DRY_RUN else "") + (" - ATTENTION" if cb.alerts else "")
        h = {"Title": title}
        if cb.alerts:
            h["Priority"] = "high"
        urllib.request.urlopen(urllib.request.Request(f"https://ntfy.sh/{cb.NTFY_TOPIC}",
                                                      data="\n".join(cb.messages[-25:]).encode(), headers=h), timeout=10)
    except Exception as e:
        print(f"(notification failed: {e})")


def cooling(s, sym):
    cd = s.get("cooldown", {}).get(sym)
    return cd and datetime.fromisoformat(cd) > datetime.now(timezone.utc)


def run(trading, cdata):
    s = cb.load_state()
    try:
        _run(trading, cdata, s)
    finally:
        cb.save_state(s)


def _run(trading, cdata, s):
    now = datetime.now(cb.ET)
    cb.say(f"PAPER | {'DRY RUN' if cb.DRY_RUN else 'LIVE ORDERS'} | meme momentum | {now:%a %b %d %I:%M %p} ET")
    for k in ("orders", "attempts", "stops", "pos"):
        s.setdefault(k, {})
    cb.settle_orders(trading, s)
    cb.settle_stops(trading, s)

    bars = cdata.get_crypto_bars(CryptoBarsRequest(symbol_or_symbols=SYMBOLS, timeframe=TimeFrame(15, TimeFrameUnit.Minute),
                                                   start=datetime.now(timezone.utc) - timedelta(days=3)))
    quotes = cdata.get_crypto_latest_quote(CryptoLatestQuoteRequest(symbol_or_symbols=SYMBOLS))
    info = {}
    for sym in SYMBOLS:
        try:
            a = trading.get_asset(sym)
            info[sym] = {"pinc": float(a.price_increment or 0), "qinc": float(a.min_trade_increment or 0)}
        except Exception:
            info[sym] = {}

    marks = {}
    for sym in SYMBOLS:
        q = quotes.get(sym)
        if q and q.bid_price and q.ask_price:
            marks[sym] = (q.bid_price + q.ask_price) / 2

    # ---- Manage the open position ----
    for sym, pos in list(s["pos"].items()):
        if sym in s["orders"] or sym not in marks:
            continue
        p = marks[sym]
        q = quotes[sym]
        pos["peak"] = max(pos.get("peak", p), p)
        opened = datetime.fromisoformat(pos.setdefault("opened", datetime.now(timezone.utc).isoformat()))
        age_h = (datetime.now(timezone.utc) - opened).total_seconds() / 3600
        chg = p / pos["entry"] - 1
        level = max(pos["peak"] * (1 - TRAIL), pos["entry"] * (1 - HARD_STOP))
        if p <= level:
            cb.place(trading, s, sym, "sell", pos["qty"], q, True, info[sym])
            cb.say(f"   reason: stop hit ({chg:+.1%})")
            s.setdefault("cooldown", {})[sym] = (datetime.now(timezone.utc) + timedelta(hours=cb.COOLDOWN_H)).isoformat()
        elif age_h >= TIME_STOP_H and chg < TIME_STOP_GAIN and (pos["peak"] / pos["entry"] - 1) < TIME_STOP_GAIN:
            cb.place(trading, s, sym, "sell", pos["qty"], q, True, info[sym])
            cb.say(f"   reason: no pump after {age_h:.0f}h ({chg:+.1%})")
            s.setdefault("cooldown", {})[sym] = (datetime.now(timezone.utc) + timedelta(hours=cb.COOLDOWN_H)).isoformat()
        else:
            cb.say(f"{sym}: holding {chg:+.1%} (peak {pos['peak'] / pos['entry'] - 1:+.1%}), stop {cb.px(level)}")
            cb.ensure_stop(trading, s, sym, level, info[sym])

    # ---- Look for the next breakout (one position at a time) ----
    if not s["pos"] and not any(o.get("side") == "buy" for o in s["orders"].values()):
        pot = s["cash"]
        if pot <= cb.BOT_BUDGET * 0.3:
            cb.alert(f"Pot is down to ${pot:.2f}. No new entries (kill switch).")
        else:
            candidates = []
            for sym in SYMBOLS:
                b = [x for x in bars.data.get(sym, []) if x.close]
                if len(b) < 100 or sym not in marks:
                    continue
                p = marks[sym]
                closes = [x.close for x in b]
                ret4h = p / closes[-17] - 1
                prior_high = max(x.high for x in b[-97:-1])
                vols = [x.volume or 0 for x in b]
                avg_vol = sum(vols[-97:-1]) / 96 or 1e-12
                surge = (sum(vols[-4:]) / 4) / avg_vol
                status = f"{sym}: 4h {ret4h:+.1%}, vol {surge:.1f}x, {'above' if p > prior_high else 'below'} 24h high"
                if cooling(s, sym):
                    cb.say(status + " (cooling down)")
                    continue
                if ret4h >= MOMENTUM_4H and p > prior_high and surge >= VOLUME_SURGE:
                    candidates.append((ret4h, sym, status))
                else:
                    cb.say(status)
            if candidates:
                candidates.sort(reverse=True)
                ret4h, sym, status = candidates[0]
                q = quotes[sym]
                amount = pot * (1 - cb.FEE_EST)
                cb.say(f"🚀 BREAKOUT: {status}")
                cb.place(trading, s, sym, "buy", amount / q.ask_price, q, True, info[sym])
                if sym in s["pos"]:
                    s["pos"][sym]["opened"] = datetime.now(timezone.utc).isoformat()
            else:
                cb.say("No breakout right now. Waiting.")

    value = s["cash"] + sum(p["qty"] * marks.get(k, p["entry"]) for k, p in s["pos"].items())
    w, l = s.get("wins", 0), s.get("losses", 0)
    cb.say(f"Pot ${value:.2f} ({value / cb.BOT_BUDGET - 1:+.1%}) | fees ≈ ${s.get('fees', 0):.2f} | "
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
