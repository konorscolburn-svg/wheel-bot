"""
Crypto Day-Trading Bot - hourly trend-following on Bitcoin & Ethereum (Alpaca, PAPER ONLY by default)
=====================================================================================================

WHAT THE RESEARCH SAYS (and how this bot uses it)
  - Intraday crypto trend signals (moving-average crossovers on hourly data) have matched
    buy-and-hold with smaller drawdowns in careful out-of-sample tests, but only when trading
    costs stay low. One 2026 study put the break-even cost near 0.4% per transaction.
    Alpaca charges 0.15% (maker) to 0.25% (taker) per trade, so the #1 design goal here is
    FEWER, better trades, and limit orders instead of market orders.
  - Simple crossovers failed on most small coins in other studies, so this sticks to
    BTC and ETH, the most liquid markets with the tightest spreads.

THE RULES (checked once an hour, 24/7)
  Enter (long only):  20-hour EMA rises above the 100-hour EMA by a small buffer AND price is
                      above the 200-hour EMA (the bigger trend agrees).
  Exit:               20-hour EMA drops below the 100-hour EMA (buffered), OR
                      price falls 3x ATR below its highest point since entry (trailing stop), OR
                      price falls 6% below entry (hard stop).
                      After a stop, that coin is paused for 12 hours (no revenge trading).
  Orders:             first try a limit order at the mid-price (cheaper maker fee); if it doesn't
                      fill by the next hour, it's cancelled and re-sent to fill immediately.
                      Stops always fill immediately.
  Fees:               the bot books an estimated 0.25% on every fill, so its reported P/L is
                      realistic even if paper trading doesn't charge fees.

SAFETY
  - Uses only BOT_BUDGET dollars (default $100), split between BTC and ETH.
  - Kill switch: no new entries once the pot is down 30%.
  - Crypto only, so it can share the swing/leverage paper account without touching their stocks.
  - PAPER=true and DRY_RUN=true by default.

Not financial advice. Crypto can drop sharply at any hour; most day-trading bots lose to fees.
"""

import csv
import json
import math
import os
import sys
import time as _time
import traceback
import urllib.request
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from alpaca.data.historical.crypto import CryptoHistoricalDataClient
from alpaca.data.requests import CryptoBarsRequest, CryptoLatestQuoteRequest
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
from alpaca.trading.client import TradingClient
from alpaca.trading.enums import OrderSide, TimeInForce
from alpaca.trading.requests import LimitOrderRequest

ET = ZoneInfo("America/New_York")


def env_bool(n, d):
    v = os.getenv(n)
    return d if v in (None, "") else v.strip().lower() in ("1", "true", "yes", "on")


def env_float(n, d):
    try:
        v = os.getenv(n)
        return float(v) if v not in (None, "") else d
    except ValueError:
        return d


# ------------------------------ SETTINGS ------------------------------
PAPER = env_bool("PAPER", True)
DRY_RUN = env_bool("DRY_RUN", True)
BOT_BUDGET = env_float("BOT_BUDGET", 100.0)
RISK = (os.getenv("CRYPTO_RISK") or "normal").strip().lower()   # "normal" or "high"
HIGH = RISK == "high"
_default_syms = "BTC/USD,ETH/USD,SOL/USD,DOGE/USD" if HIGH else "BTC/USD,ETH/USD,SOL/USD"
SYMBOLS = [s.strip().upper() for s in (os.getenv("CRYPTO_SYMBOLS") or _default_syms).split(",") if s.strip()]
SPEED = (os.getenv("CRYPTO_SPEED") or "normal").strip().lower()  # "normal" (hourly) or "turbo" (15-minute)
TURBO = SPEED == "turbo"
if TURBO:
    # 15-minute bars: 9/21 EMA crossover, trend filter = 24-hour EMA (96 bars)
    FAST, SLOW, TREND = 9, 21, 96
    BUFFER = 0.001
    ATR_MULT = 3.0 if HIGH else 2.5
    HARD_STOP = 0.08 if HIGH else 0.04
    MIN_ATR_PCT = 0.0025   # skip dead markets: a 15-min bar must typically move >= 0.25% to have a chance of beating fees
else:
    FAST, SLOW, TREND = 20, 100, 200     # hourly EMA lengths
    BUFFER = 0.002                        # 0.2% hysteresis around the crossover to cut whipsaws
    ATR_MULT = 4.0 if HIGH else 3.0       # high risk: looser trailing stop, rides bigger swings
    HARD_STOP = 0.10 if HIGH else 0.06    # high risk: tolerates a 10% drop before bailing
    MIN_ATR_PCT = 0.0
ATR_LEN = 14
ALL_IN = HIGH                             # high risk: first signal gets the whole pot, not a slice
FEE_EST = 0.0025                          # booked on every fill (Alpaca taker tier 1)
KILL_SWITCH = 0.30
COOLDOWN_H = 3 if (os.getenv("CRYPTO_SPEED") or "").lower() == "turbo" else 12  # no re-entry after a stop-out
MIN_ORDER = 5.0
NOTIFY_EVERY_RUN = env_bool("NOTIFY_EVERY_RUN", False)
NTFY_TOPIC = os.getenv("NTFY_TOPIC", "")
STATE_FILE, LOG_FILE = "state_crypto.json", "trades_crypto.csv"
TAG = "cb1"
# ----------------------------------------------------------------------

messages, alerts, events = [], [], []


def say(m):
    print(m)
    messages.append(m)


def event(m):
    say(m)
    events.append(m)


def alert(m):
    event("⚠️ " + m)
    alerts.append(m)


def ema(values, n):
    k = 2 / (n + 1)
    e = values[0]
    for v in values[1:]:
        e = v * k + e * (1 - k)
    return e


def atr(bars, n):
    trs = []
    for i in range(1, len(bars)):
        h, l, pc = bars[i].high, bars[i].low, bars[i - 1].close
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    if len(trs) < n:
        return None
    a = sum(trs[:n]) / n
    for t in trs[n:]:
        a = (a * (n - 1) + t) / n
    return a


def load_state():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except Exception:
        return {"cash": BOT_BUDGET, "pos": {}, "orders": {}, "attempts": {}, "fees": 0.0,
                "realized": 0.0, "wins": 0, "losses": 0, "last_daily": ""}


def save_state(s):
    with open(STATE_FILE, "w") as f:
        json.dump(s, f, indent=1, sort_keys=True)


def log(action, sym, qty, price, note=""):
    new = not os.path.exists(LOG_FILE)
    with open(LOG_FILE, "a", newline="") as f:
        w = csv.writer(f)
        if new:
            w.writerow(["timestamp_et", "mode", "action", "symbol", "qty", "price", "note"])
        mode = ("DRY" if DRY_RUN else "LIVE") + ("-paper" if PAPER else "-REAL")
        w.writerow([datetime.now(ET).isoformat(timespec="seconds"), mode, action, sym, f"{qty:.8f}", f"{price:.2f}", note])


def notify(force):
    if not NTFY_TOPIC or not messages or not (force or events or NOTIFY_EVERY_RUN):
        return
    try:
        h = {"Title": "Crypto bot" + (" (dry run)" if DRY_RUN else "") + (" - ATTENTION" if alerts else "")}
        if alerts:
            h["Priority"] = "high"
        urllib.request.urlopen(urllib.request.Request(f"https://ntfy.sh/{NTFY_TOPIC}",
                                                      data="\n".join(messages[-25:]).encode(), headers=h), timeout=10)
    except Exception as e:
        print(f"(notification failed: {e})")


def round_to(x, inc, up=False):
    if not inc or inc <= 0:
        return x
    f = math.ceil if up else math.floor
    return round(f(x / inc + (-1e-9 if up else 1e-9)) * inc, 10)


# ---------------------------- fills bookkeeping ----------------------------
def apply_fill(s, sym, side, qty, price):
    if qty <= 0:
        return
    fee = qty * price * FEE_EST
    s["fees"] = s.get("fees", 0.0) + fee
    if side == "buy":
        p = s["pos"].setdefault(sym, {"qty": 0.0, "cost": 0.0, "peak": price, "entry": price})
        p["qty"] += qty
        p["cost"] += qty * price + fee
        p["entry"] = p["cost"] / p["qty"]
        p["peak"] = max(p.get("peak", price), price)
        s["cash"] -= qty * price + fee
        event(f"🟢 BOUGHT {qty:.6f} {sym} @ ${price:,.2f} (fee ≈ ${fee:.2f})")
        log("buy", sym, qty, price, f"fee {fee:.4f}")
    else:
        p = s["pos"].get(sym)
        if not p:
            return
        qty = min(qty, p["qty"])
        cost_part = p["cost"] * qty / p["qty"]
        proceeds = qty * price - fee
        pnl = proceeds - cost_part
        s["cash"] += proceeds
        s["realized"] = s.get("realized", 0.0) + pnl
        s["wins" if pnl > 0 else "losses"] = s.get("wins" if pnl > 0 else "losses", 0) + 1
        p["qty"] -= qty
        p["cost"] -= cost_part
        if p["qty"] <= 1e-9:
            del s["pos"][sym]
        event(f"🔴 SOLD {qty:.6f} {sym} @ ${price:,.2f}: {pnl:+.2f} after fees")
        log("sell", sym, qty, price, f"P/L {pnl:+.4f}; fee {fee:.4f}")


def settle_orders(trading, s):
    """Book fills from last hour's orders and cancel anything still open."""
    for sym, o in list(s.get("orders", {}).items()):
        if DRY_RUN:
            del s["orders"][sym]
            continue
        try:
            od = trading.get_order_by_id(o["id"])
        except Exception as e:
            say(f"({sym}: couldn't read order: {e})")
            continue
        filled = float(od.filled_qty or 0)
        booked = o.get("booked", 0.0)
        if filled > booked and od.filled_avg_price:
            apply_fill(s, sym, o["side"], filled - booked, float(od.filled_avg_price))
            o["booked"] = filled
        status = str(od.status.value if hasattr(od.status, "value") else od.status)
        if status in ("filled", "canceled", "expired", "rejected", "done_for_day"):
            if status != "filled" and filled < float(o["qty"]):
                s["attempts"][sym] = s["attempts"].get(sym, 0) + 1
            else:
                s["attempts"][sym] = 0
            del s["orders"][sym]
        else:
            try:
                trading.cancel_order_by_id(o["id"])
                s["attempts"][sym] = s["attempts"].get(sym, 0) + 1
                say(f"{sym}: limit order didn't fill in time; cancelled, will re-send.")
            except Exception:
                pass


def place(trading, s, sym, side, qty, quote, urgent, info):
    bid, ask = quote.bid_price, quote.ask_price
    if urgent or s["attempts"].get(sym, 0) >= 1:
        price = ask * 1.002 if side == "buy" else bid * 0.998
        tif = TimeInForce.IOC
        style = "immediate"
    else:
        price = (bid + ask) / 2
        tif = TimeInForce.GTC
        style = "limit at mid"
    price = round_to(price, info.get("pinc"), up=(side == "buy"))
    if side == "sell" and not DRY_RUN:
        try:  # fees can be taken in coins, so never try to sell more than the account holds
            held = float(trading.get_open_position(sym.replace("/", "")).qty)
            if held < qty:
                s["pos"][sym]["qty"] = held
                qty = held
        except Exception:
            pass
    qty = round_to(qty, info.get("qinc"))
    if qty <= 0 or qty * price < 1:
        say(f"{sym}: order too small, skipped.")
        return
    say(f"{sym}: {side.upper()} {qty:.6f} @ ${price:,.2f} ({style})")
    if DRY_RUN:
        apply_fill(s, sym, side, qty, price)
        return
    try:
        o = trading.submit_order(LimitOrderRequest(
            symbol=sym, qty=qty, side=OrderSide.BUY if side == "buy" else OrderSide.SELL,
            time_in_force=tif, limit_price=price,
            client_order_id=f"{TAG}-{sym.replace('/', '')}-{datetime.now().strftime('%H%M%S%f')}"))
        s["orders"][sym] = {"id": str(o.id), "side": side, "qty": qty, "booked": 0.0, "notional": qty * price}
        if tif == TimeInForce.IOC:
            _time.sleep(3)
            settle_orders(trading, s)
    except Exception as e:
        alert(f"Order failed for {sym}: {e}")


# ---------------------------------- main ----------------------------------
def run(trading, cdata):
    s = load_state()
    try:
        _run(trading, cdata, s)
    finally:
        save_state(s)


def _run(trading, cdata, s):
    now = datetime.now(ET)
    say(f"{'PAPER' if PAPER else 'REAL MONEY'} | {'DRY RUN' if DRY_RUN else 'LIVE ORDERS'} | risk {RISK} | speed {SPEED} | {now:%a %b %d %I:%M %p} ET")
    s.setdefault("orders", {})
    s.setdefault("attempts", {})
    settle_orders(trading, s)

    tf = TimeFrame(15, TimeFrameUnit.Minute) if TURBO else TimeFrame.Hour
    lookback = timedelta(days=6) if TURBO else timedelta(days=30)
    bars = cdata.get_crypto_bars(CryptoBarsRequest(symbol_or_symbols=SYMBOLS, timeframe=tf,
                                                   start=datetime.now(timezone.utc) - lookback))
    quotes = cdata.get_crypto_latest_quote(CryptoLatestQuoteRequest(symbol_or_symbols=SYMBOLS))

    info = {}
    for sym in SYMBOLS:
        try:
            a = trading.get_asset(sym)
            info[sym] = {"pinc": float(a.price_increment or 0), "qinc": float(a.min_trade_increment or 0)}
        except Exception:
            info[sym] = {}

    marks, pot = {}, s["cash"]
    for sym in SYMBOLS:
        q = quotes.get(sym)
        if q and q.bid_price and q.ask_price:
            marks[sym] = (q.bid_price + q.ask_price) / 2
    for sym, p in s["pos"].items():
        pot += p["qty"] * marks.get(sym, p["entry"])
    halted = pot <= BOT_BUDGET * (1 - KILL_SWITCH)
    if halted:
        alert(f"KILL SWITCH: pot is ${pot:.2f} ({pot / BOT_BUDGET - 1:+.0%}). No new entries.")

    slot = BOT_BUDGET if ALL_IN else BOT_BUDGET / len(SYMBOLS)
    free = s["cash"] - sum(o.get("notional", 0) for o in s["orders"].values() if o.get("side") == "buy")
    for sym in SYMBOLS:
        b = [x for x in bars.data.get(sym, []) if x.close]
        q = quotes.get(sym)
        if len(b) < TREND + 5 or not q or not q.bid_price or not q.ask_price:
            say(f"{sym}: not enough data yet ({len(b)} bars).")
            continue
        if sym in s["orders"]:
            continue
        closes = [x.close for x in b]
        px = marks[sym]
        f, sl, tr = ema(closes, FAST), ema(closes, SLOW), ema(closes, TREND)
        a = atr(b, ATR_LEN) or px * 0.01
        pos = s["pos"].get(sym)

        if pos:
            pos["peak"] = max(pos.get("peak", px), px)
            trail = pos["peak"] - ATR_MULT * a
            chg = px / pos["entry"] - 1
            if px <= pos["entry"] * (1 - HARD_STOP) or px <= trail:
                why = f"hard stop ({chg:+.1%})" if px <= pos["entry"] * (1 - HARD_STOP) else f"trailing stop (peak ${pos['peak']:,.0f})"
                place(trading, s, sym, "sell", pos["qty"], q, True, info[sym])
                say(f"   reason: {why}; pausing new entries for {COOLDOWN_H}h")
                s.setdefault("cooldown", {})[sym] = (datetime.now(timezone.utc) + timedelta(hours=COOLDOWN_H)).isoformat()
            elif f < sl * (1 - BUFFER):
                place(trading, s, sym, "sell", pos["qty"], q, False, info[sym]); say("   reason: trend turned down")
            else:
                say(f"{sym}: holding, {chg:+.2%} vs entry, stop ${trail:,.0f}")
        else:
            up = f > sl * (1 + BUFFER) and px > tr and (a / px) >= MIN_ATR_PCT
            cd = s.get("cooldown", {}).get(sym)
            if up and cd and datetime.fromisoformat(cd) > datetime.now(timezone.utc):
                say(f"{sym}: signal, but cooling down after a stop until {datetime.fromisoformat(cd).astimezone(ET):%a %I:%M %p} ET.")
            elif up and not halted:
                amount = min(slot, free) * (1 - FEE_EST)
                if amount < MIN_ORDER:
                    say(f"{sym}: signal, but only ${max(free, 0):.2f} free in the pot.")
                else:
                    place(trading, s, sym, "buy", amount / q.ask_price, q, False, info[sym])
                    free -= amount / (1 - FEE_EST)
                    say(f"   reason: fast EMA({FAST}) above slow EMA({SLOW}) and price above trend EMA({TREND})")
            else:
                trend = "up" if f > sl else "down"
                quiet = ", market too quiet" if MIN_ATR_PCT and (a / px) < MIN_ATR_PCT else ""
                say(f"{sym}: no entry (short-term trend {trend}, {'above' if px > tr else 'below'} trend EMA{quiet})")

    pot = s["cash"] + sum(p["qty"] * marks.get(k, p["entry"]) for k, p in s["pos"].items())
    w, l = s.get("wins", 0), s.get("losses", 0)
    say(f"Pot ${pot:.2f} ({pot / BOT_BUDGET - 1:+.2%}) | fees so far ≈ ${s.get('fees', 0):.2f} | "
        f"trades {w + l}, win rate {f'{w / (w + l):.0%}' if w + l else 'n/a'}")
    return now


def main():
    k, sec = os.getenv("ALPACA_API_KEY"), os.getenv("ALPACA_SECRET_KEY")
    if not k or not sec:
        sys.exit("Missing ALPACA_API_KEY / ALPACA_SECRET_KEY.")
    run(TradingClient(k, sec, paper=PAPER), CryptoHistoricalDataClient(k, sec))


if __name__ == "__main__":
    daily = False
    try:
        main()
        st = load_state()
        today = datetime.now(ET).strftime("%Y-%m-%d")
        if datetime.now(ET).hour >= 20 and st.get("last_daily") != today:
            daily = True
            st["last_daily"] = today
            save_state(st)
    except SystemExit:
        raise
    except Exception as e:
        alert(f"BOT ERROR: {e}")
        traceback.print_exc()
        raise
    finally:
        notify(daily)
