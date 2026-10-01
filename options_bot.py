"""
Options Bot v2 - intraday "noise area" momentum, traded with short-dated options (Alpaca, PAPER ONLY)
=====================================================================================================

SIGNAL (Zarattini, Aziz & Barbon 2024, "Beat the Market: An Effective Intraday Momentum
Strategy for S&P500 ETF (SPY)": ~19.6%/yr net of costs, Sharpe 1.33, 2007-2024):
  - "Noise area" = today's open +/- the average absolute move from the open at THIS time of day
    over the last 14 sessions (gap-adjusted with yesterday's close). Inside it = noise, no trade.
  - Decisions every 30 minutes from 10:00 to 15:30.
  - Price breaks ABOVE the upper band -> buy CALLS.  BELOW the lower band -> buy PUTS.
  - Exit when price falls back through the band or VWAP (whichever is tighter), and ALWAYS flat
    by the 15:30 decision (never held overnight).

HOW THIS BOT ADDS LEVERAGE: instead of shares, it buys an option on SPY or QQQ:
  - expiring 1-7 days out (never same-day 0DTE)
  - as close to the money as the pot can afford (delta target 0.45): research shows far
    out-of-the-money options have the worst average returns for buyers
  - option-level guards: -50% disaster stop and +150% take-profit on the option price

MORE TRADES: can re-enter after an exit (up to 3 entries per day), and watches both SPY and QQQ,
taking whichever breakout is strongest.

AUDIT NOTES (independent replication, codecat-ops/zarattini-2024-momentum-spy): replicated Sharpe
1.11, win rate 41%, winners ~1.7x losers, but the edge has compressed since 2025 (recent Sharpe ~0).
Parameter tweaks did NOT help; the paper's original settings (used here) still won.

Reality check: the research tested SHARES. Options add leverage AND time decay, and option buyers
lose on average. Paper only. Not financial advice.
"""

import csv
import json
import math
import os
import re
import sys
import traceback
import urllib.request
from datetime import date, datetime, time, timedelta, timezone
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
UNDERLYINGS = [s.strip().upper() for s in (os.getenv("OPT_SYMBOLS") or "SPY,QQQ").split(",") if s.strip()]
LOOKBACK_DAYS = 14                 # sessions used to build the noise area (as in the paper)
BAND_MULT = 1.0
FIRST_DECISION, LAST_ENTRY, FLAT_BY = time(10, 0), time(15, 0), time(15, 30)
MAX_ENTRIES_PER_DAY = 3
MIN_DTE, MAX_DTE = 1, 7
DELTA_LO, DELTA_TARGET, DELTA_HI = 0.15, 0.45, 0.65
OPT_STOP, OPT_TAKE = 0.50, 1.50   # disaster stop only; the strategy's real exit is band/VWAP
MAX_SPREAD = 0.20
RISK_FREE = 0.04
OPTIONS_FEED = OptionsFeed.OPRA if os.getenv("OPTIONS_FEED", "").lower() == "opra" else OptionsFeed.INDICATIVE
NTFY_TOPIC = os.getenv("NTFY_TOPIC", "")
NOTIFY_EVERY_RUN = env_bool("NOTIFY_EVERY_RUN", False)
STATE_FILE, LOG_FILE = "state_options.json", "trades_options.csv"
TAG = "ob2"
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
            s = json.load(f)
    except Exception:
        s = {}
    s.setdefault("cash", BOT_BUDGET)
    s.setdefault("pos", None)
    s.setdefault("order", None)
    for k in ("realized", "wins", "losses", "retry"):
        s.setdefault(k, 0)
    return s


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


# ------------------------------ noise area ------------------------------
def noise_area(bars, now):
    """Return dict(upper, lower, vwap, price, sigma) for today's session, or None."""
    today = now.date()
    by_day = {}
    for b in bars:
        t = b.timestamp.astimezone(ET)
        if time(9, 30) <= t.time() < time(16, 0):
            by_day.setdefault(t.date(), []).append((t, b))
    days = sorted(by_day)
    if today not in by_day or len(days) < LOOKBACK_DAYS + 2:
        return None
    t_bars = by_day[today]
    minute_now = (now.hour * 60 + now.minute) - (9 * 60 + 30)

    def at_minute(day_bars, m):
        cut = None
        for t, b in day_bars:
            if (t.hour * 60 + t.minute) - (9 * 60 + 30) <= m:
                cut = b
            else:
                break
        return cut

    past = [d for d in days if d < today][-LOOKBACK_DAYS:]
    moves = []
    for d in past:
        db = by_day[d]
        b = at_minute(db, minute_now)
        if b and db:
            moves.append(abs(b.close / db[0][1].open - 1))
    if len(moves) < LOOKBACK_DAYS // 2:
        return None
    sigma = sum(moves) / len(moves) * BAND_MULT
    prev_close = by_day[past[-1]][-1][1].close
    open_ = t_bars[0][1].open
    vol = sum((b.volume or 0) for _, b in t_bars)
    vwap = (sum((b.vwap or b.close) * (b.volume or 0) for _, b in t_bars) / vol) if vol else t_bars[-1][1].close
    return dict(upper=max(open_, prev_close) * (1 + sigma), lower=min(open_, prev_close) * (1 - sigma),
                vwap=vwap, price=t_bars[-1][1].close, sigma=sigma)


# ---------------------------- orders & fills ----------------------------
def quote_for(odata, sym):
    info = parse_occ(sym)
    try:
        ch = odata.get_option_chain(OptionChainRequest(
            underlying_symbol=info[0], feed=OPTIONS_FEED, expiration_date=info[1],
            type=ContractType.CALL if info[2] == "C" else ContractType.PUT,
            strike_price_gte=info[3] - 0.01, strike_price_lte=info[3] + 0.01))
        q = getattr(ch.get(sym), "latest_quote", None)
        if q and q.ask_price and q.ask_price > 0:
            return float(q.bid_price or 0), float(q.ask_price)
    except Exception as e:
        say(f"(quote failed for {sym}: {e})")
    return None


def book(s, side, sym, qty, price, why):
    cost = qty * price * 100
    if side == "buy":
        s["cash"] -= cost
        s["pos"] = {"symbol": sym, "qty": qty, "entry": price, "dir": parse_occ(sym)[2]}
        event(f"🟢 BOUGHT {qty}x {sym} @ ${price:.2f} (${cost:.0f}) - {why}")
        log("buy", sym, qty, price, why)
    else:
        p = s.get("pos") or {"entry": price, "qty": qty}
        pnl = (price - p["entry"]) * qty * 100
        s["cash"] += cost
        s["realized"] += pnl
        s["wins" if pnl > 0 else "losses"] += 1
        p["qty"] -= qty
        if p["qty"] <= 0:
            s["pos"] = None
        event(f"🔴 SOLD {qty}x {sym} @ ${price:.2f}: {pnl:+.2f} ({why})")
        log("sell", sym, qty, price, f"{why}; P/L {pnl:+.2f}")


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
        s["retry"] += 1


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


def pick_contract(odata, u, cp, spot, today, cash):
    ch = odata.get_option_chain(OptionChainRequest(
        underlying_symbol=u, feed=OPTIONS_FEED, type=ContractType.CALL if cp == "C" else ContractType.PUT,
        expiration_date_gte=today + timedelta(days=MIN_DTE), expiration_date_lte=today + timedelta(days=MAX_DTE),
        strike_price_gte=spot * 0.95, strike_price_lte=spot * 1.05))
    best = None
    for sym, snap in (ch or {}).items():
        info = parse_occ(sym)
        q = getattr(snap, "latest_quote", None)
        if not info or not q or not q.ask_price or not q.bid_price or q.bid_price <= 0:
            continue
        bid, ask = float(q.bid_price), float(q.ask_price)
        mid = (bid + ask) / 2
        if (ask - bid) / mid > MAX_SPREAD or ask * 100 > cash:
            continue
        dte = (info[1] - today).days
        T = max(dte, 1) / 365
        g = getattr(snap, "greeks", None)
        d = getattr(g, "delta", None) if g else None
        if d is None:
            iv = getattr(snap, "implied_volatility", None) or implied_vol(cp, mid, spot, info[3], T)
            if not iv:
                continue
            d = bs_delta(cp, spot, info[3], T, iv)
        if not (DELTA_LO <= abs(d) <= DELTA_HI):
            continue
        # prefer delta near target, then the nearest expiry (most leverage per dollar)
        score = abs(abs(d) - DELTA_TARGET) + dte * 0.01
        if best is None or score < best[0]:
            best = (score, sym, bid, ask, mid, d)
    return best


def run(trading, odata, sdata):
    s = load_state()
    try:
        _run(trading, odata, sdata, s)
    finally:
        save_state(s)


def _run(trading, odata, sdata, s):
    now = datetime.now(ET)
    today = now.date()
    say(f"PAPER | {'DRY RUN' if DRY_RUN else 'LIVE ORDERS'} | noise-area momentum | {now:%a %b %d %I:%M %p} ET")
    if not trading.get_clock().is_open:
        say("Market closed.")
        return
    if s.get("day") != today.isoformat():
        s["day"], s["entries_today"] = today.isoformat(), 0
    settle(trading, s)

    bars = sdata.get_stock_bars(StockBarsRequest(symbol_or_symbols=UNDERLYINGS, timeframe=TimeFrame.Minute,
                                                 feed=DataFeed.IEX,
                                                 start=datetime.now(timezone.utc) - timedelta(days=LOOKBACK_DAYS * 2 + 6)))
    zones = {}
    for u in UNDERLYINGS:
        z = noise_area(bars.data.get(u, []), now)
        if z:
            zones[u] = z

    # ---- Manage the open option ----
    pos = s.get("pos")
    if pos and not s.get("order"):
        sym = pos["symbol"]
        u, exp, cp, _ = parse_occ(sym)
        q = quote_for(odata, sym)
        z = zones.get(u)
        why = None
        if now.time() >= FLAT_BY:
            why = "flat before the close"
        elif z:
            if cp == "C" and z["price"] < max(z["upper"], z["vwap"]):
                why = f"{u} fell back below band/VWAP"
            elif cp == "P" and z["price"] > min(z["lower"], z["vwap"]):
                why = f"{u} rose back above band/VWAP"
        if q:
            bid, ask = q
            mid = (bid + ask) / 2 if bid > 0 else ask / 2
            chg = mid / pos["entry"] - 1
            if not why and chg <= -OPT_STOP:
                why = f"option stop {chg:+.0%}"
            elif not why and chg >= OPT_TAKE:
                why = f"take profit {chg:+.0%}"
            if why:
                limit = bid if s["retry"] == 0 else max(0.01, bid * 0.9)
                submit(trading, s, "sell", sym, pos["qty"], limit, why)
            else:
                say(f"{sym}: holding {chg:+.0%} (mid ${mid:.2f}); {u} still outside the noise area")
        elif why:
            alert(f"No quote to exit {sym} ({why}); will retry next run.")
        pos = s.get("pos")

    # ---- New entries ----
    if not pos and not s.get("order"):
        if s["cash"] <= BOT_BUDGET * 0.2:
            alert(f"Pot is down to ${s['cash']:.2f}. No new trades (kill switch).")
        elif now.time() < FIRST_DECISION or now.time() > LAST_ENTRY:
            say("Outside the entry window (10:00 AM - 3:00 PM ET).")
        elif s["entries_today"] >= MAX_ENTRIES_PER_DAY:
            say(f"Already made {MAX_ENTRIES_PER_DAY} entries today.")
        else:
            last = sdata.get_stock_latest_trade(StockLatestTradeRequest(symbol_or_symbols=list(zones) or UNDERLYINGS,
                                                                        feed=DataFeed.IEX))
            picks = []
            for u, z in zones.items():
                p = float(last[u].price) if u in last else z["price"]
                if p > z["upper"] and p > z["vwap"]:
                    picks.append(((p / z["upper"] - 1) / z["sigma"], u, "C", p, z))
                elif p < z["lower"] and p < z["vwap"]:
                    picks.append(((1 - p / z["lower"]) / z["sigma"], u, "P", p, z))
                else:
                    say(f"{u} ${p:.2f} inside noise area (${z['lower']:.2f} - ${z['upper']:.2f}). No trade.")
            picks.sort(reverse=True)
            for strength, u, cp, spot, z in picks:
                best = pick_contract(odata, u, cp, spot, today, s["cash"])
                if not best:
                    say(f"{u}: {'CALL' if cp == 'C' else 'PUT'} signal, but no affordable contract (try a bigger OPT_BUDGET).")
                    continue
                _, sym, bid, ask, mid, d = best
                qty = max(1, int(s["cash"] // (ask * 100)))
                limit = mid if s["retry"] == 0 else ask
                event(f"🎯 {u} broke {'ABOVE' if cp == 'C' else 'BELOW'} its noise area "
                      f"(${spot:.2f} vs {'upper' if cp == 'C' else 'lower'} ${z['upper'] if cp == 'C' else z['lower']:.2f})")
                submit(trading, s, "buy", sym, qty, limit, f"delta {abs(d):.2f}")
                s["entries_today"] += 1
                break
            if not zones:
                say("Not enough intraday history yet to build the noise area.")

    if not s.get("order") and not s.get("pos"):
        s["retry"] = 0
    pos = s.get("pos")
    say(f"Pot ${s['cash']:.2f} cash{' + ' + pos['symbol'] if pos else ''} | realized {s['realized']:+.2f} | "
        f"trades {s['wins'] + s['losses']}, wins {s['wins']} | entries today {s.get('entries_today', 0)}")


def main():
    k, sec = os.getenv("ALPACA_API_KEY"), os.getenv("ALPACA_SECRET_KEY")
    if not k or not sec:
        sys.exit("Missing ALPACA_API_KEY / ALPACA_SECRET_KEY.")
    run(TradingClient(k, sec, paper=True), OptionHistoricalDataClient(k, sec), StockHistoricalDataClient(k, sec))


if __name__ == "__main__":
    daily = False
    try:
        main()
        daily = datetime.now(ET).time() >= time(15, 30)
    except SystemExit:
        raise
    except Exception as e:
        alert(f"BOT ERROR: {e}")
        traceback.print_exc()
        raise
    finally:
        notify(daily)
