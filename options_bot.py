"""
Weekly Options Bot - high-risk momentum option BUYING (Alpaca, PAPER ONLY)
==========================================================================

Buys cheap, short-dated calls when an index ETF breaks out up, or puts when it breaks down.
These are lottery-style trades: most lose, a few pay 2x or more.

UNIVERSE: SPY, QQQ, IWM (the most liquid options, penny-wide prices).

SIGNAL (checked every 30 minutes during market hours):
  CALL: price above its 20-day average, above yesterday's high, and up 0.4%+ today
  PUT:  price below its 20-day average, below yesterday's low, and down 0.4%+ today
  If several qualify, it takes the strongest move.

CONTRACT: expires in 2-7 days, delta about 0.25 (out of the money), total cost within the pot.

POSITION: one at a time, the whole pot (all-in).

EXITS:
  - take profit at +100% (the option doubled)
  - stop loss at -50%
  - always sold by 3:00 PM the day BEFORE expiration (never held into expiration day)

Uses its own Black-Scholes math when the free data feed omits delta/IV.
Hard-locked to paper trading. Not financial advice.
"""

import csv
import json
import math
import os
import re
import sys
import traceback
import urllib.request
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from alpaca.data.enums import DataFeed, OptionsFeed
from alpaca.data.historical.option import OptionHistoricalDataClient
from alpaca.data.historical.stock import StockHistoricalDataClient
from alpaca.data.requests import OptionChainRequest, StockBarsRequest, StockLatestTradeRequest
from alpaca.data.timeframe import TimeFrame
from alpaca.trading.client import TradingClient
from alpaca.trading.enums import ContractType, OrderSide, PositionIntent, TimeInForce
from alpaca.trading.requests import LimitOrderRequest

ET = ZoneInfo("America/New_York")
PAPER = True                                       # hard-locked


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
DRY_RUN = env_bool("DRY_RUN", True)
BOT_BUDGET = env_float("BOT_BUDGET", 100.0)
UNDERLYINGS = [s.strip().upper() for s in (os.getenv("OPT_SYMBOLS") or "SPY,QQQ,IWM").split(",") if s.strip()]
MIN_DTE, MAX_DTE = 2, 7
DELTA_LO, DELTA_TARGET, DELTA_HI = 0.15, 0.25, 0.35
MIN_MOVE = 0.004                  # 0.4% move today
TAKE_PROFIT, STOP_LOSS = 1.00, 0.50
MAX_SPREAD = 0.25                 # skip contracts whose bid/ask spread is > 25% of mid
RISK_FREE = 0.04
OPTIONS_FEED = OptionsFeed.OPRA if os.getenv("OPTIONS_FEED", "").lower() == "opra" else OptionsFeed.INDICATIVE
NTFY_TOPIC = os.getenv("NTFY_TOPIC", "")
NOTIFY_EVERY_RUN = env_bool("NOTIFY_EVERY_RUN", False)
STATE_FILE, LOG_FILE = "state_options.json", "trades_options.csv"
TAG = "ob1"
# ----------------------------------------------------------------------

OCC = re.compile(r"^([A-Z.]{1,6})(\d{6})([CP])(\d{8})$")
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


def parse_occ(sym):
    m = OCC.match(sym or "")
    if not m:
        return None
    root, ymd, cp, k = m.groups()
    return root, datetime.strptime(ymd, "%y%m%d").date(), cp, int(k) / 1000


def _n(x):
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def bs_price(cp, S, K, T, v):
    d1 = (math.log(S / K) + (RISK_FREE + v * v / 2) * T) / (v * math.sqrt(T))
    d2 = d1 - v * math.sqrt(T)
    if cp == "C":
        return S * _n(d1) - K * math.exp(-RISK_FREE * T) * _n(d2)
    return K * math.exp(-RISK_FREE * T) * _n(-d2) - S * _n(-d1)


def bs_delta(cp, S, K, T, v):
    d1 = (math.log(S / K) + (RISK_FREE + v * v / 2) * T) / (v * math.sqrt(T))
    return _n(d1) if cp == "C" else _n(d1) - 1


def implied_vol(cp, price, S, K, T):
    lo, hi = 0.01, 4.0
    try:
        if bs_price(cp, S, K, T, hi) < price or bs_price(cp, S, K, T, lo) > price:
            return None
        for _ in range(60):
            mid = (lo + hi) / 2
            lo, hi = (lo, mid) if bs_price(cp, S, K, T, mid) > price else (mid, hi)
        return (lo + hi) / 2
    except (ValueError, ZeroDivisionError):
        return None


def load_state():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except Exception:
        return {"cash": BOT_BUDGET, "pos": None, "order": None, "realized": 0.0, "wins": 0, "losses": 0}


def save_state(s):
    with open(STATE_FILE, "w") as f:
        json.dump(s, f, indent=1, sort_keys=True)


def log(action, sym, qty, price, note=""):
    new = not os.path.exists(LOG_FILE)
    with open(LOG_FILE, "a", newline="") as f:
        w = csv.writer(f)
        if new:
            w.writerow(["timestamp_et", "mode", "action", "symbol", "qty", "price", "note"])
        w.writerow([datetime.now(ET).isoformat(timespec="seconds"), "DRY" if DRY_RUN else "LIVE-paper",
                    action, sym, qty, f"{price:.2f}", note])


def notify(force):
    if not NTFY_TOPIC or not messages or not (force or events or NOTIFY_EVERY_RUN):
        return
    try:
        h = {"Title": "Options bot" + (" (dry run)" if DRY_RUN else "") + (" - ATTENTION" if alerts else "")}
        if alerts:
            h["Priority"] = "high"
        urllib.request.urlopen(urllib.request.Request(f"https://ntfy.sh/{NTFY_TOPIC}",
                                                      data="\n".join(messages[-25:]).encode(), headers=h), timeout=10)
    except Exception as e:
        print(f"(notification failed: {e})")


def quote_for(odata, sym):
    info = parse_occ(sym)
    try:
        ch = odata.get_option_chain(OptionChainRequest(
            underlying_symbol=info[0], feed=OPTIONS_FEED, expiration_date=info[1],
            type=ContractType.CALL if info[2] == "C" else ContractType.PUT,
            strike_price_gte=info[3] - 0.01, strike_price_lte=info[3] + 0.01))
        q = getattr(ch.get(sym), "latest_quote", None)
        if q and q.bid_price is not None and q.ask_price and q.ask_price > 0:
            return float(q.bid_price or 0), float(q.ask_price)
    except Exception as e:
        say(f"(quote failed for {sym}: {e})")
    return None


# ---------------------------- orders & fills ----------------------------
def settle(trading, s):
    o = s.get("order")
    if not o:
        return
    if DRY_RUN:
        s["order"] = None
        return
    try:
        od = trading.get_order_by_id(o["id"])
    except Exception as e:
        say(f"(couldn't read order: {e})")
        return
    filled = int(float(od.filled_qty or 0))
    status = str(od.status.value if hasattr(od.status, "value") else od.status)
    if filled > o.get("booked", 0) and od.filled_avg_price:
        book(s, o["side"], o["symbol"], filled - o.get("booked", 0), float(od.filled_avg_price), o.get("why", ""))
        o["booked"] = filled
    if status in ("filled", "canceled", "expired", "rejected", "done_for_day"):
        s["order"] = None
        if status == "filled":
            s["retry"] = 0
    else:
        try:
            trading.cancel_order_by_id(o["id"])
            say(f"{o['symbol']}: order didn't fill; cancelled, will re-price.")
        except Exception:
            pass
        s["order"] = None
        s["retry"] = s.get("retry", 0) + 1


def book(s, side, sym, qty, price, why):
    cost = qty * price * 100
    if side == "buy":
        s["cash"] -= cost
        s["pos"] = {"symbol": sym, "qty": qty, "entry": price, "opened": datetime.now(timezone.utc).isoformat()}
        event(f"🟢 BOUGHT {qty}x {sym} @ ${price:.2f} (${cost:.0f})")
        log("buy", sym, qty, price, why)
    else:
        p = s.get("pos") or {"entry": price, "qty": qty}
        pnl = (price - p["entry"]) * qty * 100
        s["cash"] += cost
        s["realized"] = s.get("realized", 0) + pnl
        s["wins" if pnl > 0 else "losses"] += 1
        p["qty"] -= qty
        if p["qty"] <= 0:
            s["pos"] = None
        event(f"🔴 SOLD {qty}x {sym} @ ${price:.2f}: {pnl:+.2f} ({why})")
        log("sell", sym, qty, price, f"{why}; P/L {pnl:+.2f}")


def submit(trading, s, side, sym, qty, limit, why):
    limit = max(0.01, round(limit, 2))
    say(f"{sym}: {side.upper()} {qty}x @ ${limit:.2f} ({why})")
    if DRY_RUN:
        book(s, side, sym, qty, limit, why)
        return
    try:
        o = trading.submit_order(LimitOrderRequest(
            symbol=sym, qty=qty, side=OrderSide.BUY if side == "buy" else OrderSide.SELL,
            time_in_force=TimeInForce.DAY, limit_price=limit,
            position_intent=PositionIntent.BUY_TO_OPEN if side == "buy" else PositionIntent.SELL_TO_CLOSE,
            client_order_id=f"{TAG}-{datetime.now().strftime('%H%M%S%f')}"))
        s["order"] = {"id": str(o.id), "side": side, "symbol": sym, "qty": qty, "booked": 0, "why": why}
    except Exception as e:
        alert(f"Order failed for {sym}: {e}")


# --------------------------------- main ---------------------------------
def run(trading, odata, sdata):
    s = load_state()
    try:
        _run(trading, odata, sdata, s)
    finally:
        save_state(s)


def _run(trading, odata, sdata, s):
    now = datetime.now(ET)
    today = now.date()
    say(f"PAPER | {'DRY RUN' if DRY_RUN else 'LIVE ORDERS'} | weekly options | {now:%a %b %d %I:%M %p} ET")
    if not trading.get_clock().is_open:
        say("Market closed.")
        return
    settle(trading, s)
    retry = s.get("retry", 0)

    # ---- Manage the open option ----
    pos = s.get("pos")
    if pos and not s.get("order"):
        sym = pos["symbol"]
        _, exp, _, _ = parse_occ(sym)
        q = quote_for(odata, sym)
        if q:
            bid, ask = q
            mid = (bid + ask) / 2 if bid > 0 else ask / 2
            chg = mid / pos["entry"] - 1
            days_left = (exp - today).days
            why = None
            if chg >= TAKE_PROFIT:
                why = f"take profit {chg:+.0%}"
            elif chg <= -STOP_LOSS:
                why = f"stop loss {chg:+.0%}"
            elif days_left <= 0 or (days_left == 1 and now.hour >= 15):
                why = f"expires {'today' if days_left <= 0 else 'tomorrow'} ({chg:+.0%})"
            if why:
                limit = bid if retry == 0 else max(0.01, bid * 0.9)
                submit(trading, s, "sell", sym, pos["qty"], limit if limit > 0 else 0.01, why)
            else:
                say(f"{sym}: holding {chg:+.0%} (mid ${mid:.2f}), {days_left}d left")
        pos = s.get("pos")

    # ---- Look for a new trade (one at a time) ----
    if not pos and not s.get("order"):
        if s["cash"] <= BOT_BUDGET * 0.2:
            alert(f"Pot is down to ${s['cash']:.2f}. No new trades (kill switch).")
        elif now.hour == 15 and now.minute >= 30:
            say("Too late in the day for new entries.")
        else:
            bars = sdata.get_stock_bars(StockBarsRequest(symbol_or_symbols=UNDERLYINGS, timeframe=TimeFrame.Day,
                                                         feed=DataFeed.IEX,
                                                         start=datetime.now(timezone.utc) - timedelta(days=45)))
            last = sdata.get_stock_latest_trade(StockLatestTradeRequest(symbol_or_symbols=UNDERLYINGS, feed=DataFeed.IEX))
            picks = []
            for u in UNDERLYINGS:
                hist = [b for b in bars.data.get(u, []) if b.timestamp.astimezone(ET).date() < today]
                if len(hist) < 21 or u not in last:
                    continue
                p = float(last[u].price)
                sma20 = sum(b.close for b in hist[-20:]) / 20
                prev = hist[-1]
                move = p / prev.close - 1
                if p > sma20 and p > prev.high and move >= MIN_MOVE:
                    picks.append((abs(move), u, "C", p, move))
                elif p < sma20 and p < prev.low and move <= -MIN_MOVE:
                    picks.append((abs(move), u, "P", p, move))
                else:
                    say(f"{u}: {move:+.2%} today, {'above' if p > sma20 else 'below'} 20-day avg. No signal.")
            picks.sort(reverse=True)
            placed = False
            for _, u, cp, spot, move in picks:
                ch = odata.get_option_chain(OptionChainRequest(
                    underlying_symbol=u, feed=OPTIONS_FEED,
                    type=ContractType.CALL if cp == "C" else ContractType.PUT,
                    expiration_date_gte=today + timedelta(days=MIN_DTE),
                    expiration_date_lte=today + timedelta(days=MAX_DTE),
                    strike_price_gte=spot * (1.0 if cp == "C" else 0.9),
                    strike_price_lte=spot * (1.1 if cp == "C" else 1.0)))
                best = None
                for sym, snap in (ch or {}).items():
                    info = parse_occ(sym)
                    q = getattr(snap, "latest_quote", None)
                    if not info or not q or not q.ask_price or not q.bid_price or q.bid_price <= 0:
                        continue
                    bid, ask = float(q.bid_price), float(q.ask_price)
                    mid = (bid + ask) / 2
                    if (ask - bid) / mid > MAX_SPREAD or ask * 100 > s["cash"]:
                        continue
                    T = max((info[1] - today).days, 1) / 365
                    g = getattr(snap, "greeks", None)
                    d = getattr(g, "delta", None) if g else None
                    if d is None:
                        iv = getattr(snap, "implied_volatility", None) or implied_vol(cp, mid, spot, info[3], T)
                        if not iv:
                            continue
                        d = bs_delta(cp, spot, info[3], T, iv)
                    if not (DELTA_LO <= abs(d) <= DELTA_HI):
                        continue
                    score = abs(abs(d) - DELTA_TARGET)
                    if best is None or score < best[0]:
                        best = (score, sym, bid, ask, mid, d)
                if not best:
                    say(f"{u}: {'CALL' if cp == 'C' else 'PUT'} signal ({move:+.2%}), but no affordable contract fits.")
                    continue
                _, sym, bid, ask, mid, d = best
                qty = max(1, int(s["cash"] // (ask * 100)))
                limit = mid if retry == 0 else ask
                event(f"🎯 {u} {'breakout UP' if cp == 'C' else 'breakdown DOWN'} ({move:+.2%})")
                submit(trading, s, "buy", sym, qty, limit, f"delta {abs(d):.2f}")
                placed = True
                break
            if not picks:
                say("No breakout signal right now.")

    if not s.get("order") and not s.get("pos"):
        s["retry"] = 0
    pos = s.get("pos")
    say(f"Pot: ${s['cash']:.2f} cash{' + open ' + pos['symbol'] if pos else ''} | realized {s.get('realized', 0):+.2f} | "
        f"trades {s.get('wins', 0) + s.get('losses', 0)}, wins {s.get('wins', 0)}")


def main():
    k, sec = os.getenv("ALPACA_API_KEY"), os.getenv("ALPACA_SECRET_KEY")
    if not k or not sec:
        sys.exit("Missing ALPACA_API_KEY / ALPACA_SECRET_KEY.")
    run(TradingClient(k, sec, paper=True), OptionHistoricalDataClient(k, sec), StockHistoricalDataClient(k, sec))


if __name__ == "__main__":
    daily = False
    try:
        main()
        daily = datetime.now(ET).hour == 15 and datetime.now(ET).minute >= 30
    except SystemExit:
        raise
    except Exception as e:
        alert(f"BOT ERROR: {e}")
        traceback.print_exc()
        raise
    finally:
        notify(daily)
