"""
Wheel Bot v3 (Alpaca) - automated cash-secured puts + covered calls
==================================================================

THE CYCLE, per ticker in WATCHLIST:
  No shares -> sell a cash-secured PUT below the price. Assigned? You now own 100 shares.
  Shares    -> sell a covered CALL above the price (and above your cost). Called away? Back to puts.

RULES AND WHY (sources summarized in the chat that delivered this file)
  Entry
  - ~45 days to expiration (window 30-50), put delta ~0.25, call delta ~0.30.
  - Only sell when options are priced above the stock's actual recent movement
    (implied vol / 20-day realized vol). That gap, the volatility risk premium, is the
    documented source of put-selling returns.
  - Once ~20 trading days of history are saved, also skip puts when a ticker's implied vol is
    near the bottom of its own range (low IV rank = thin premium).
  - Market stress switch: if SPY's 1-month implied vol is above its 3-month implied vol
    (an inverted "term structure", a classic panic signal), or SPY just had a huge down day,
    no new puts anywhere until it normalizes.
  - Downtrend mode: below the 200-day average, puts are sold further out of the money.
  - Repair mode: if shares are below your cost, calls are sold at or above your cost at low delta,
    so you never lock in a loss by being called away.
  Exits
  - Buy back at 50% profit. At 21 days left, close anything with at least 10% profit and redeploy.
  - Losing positions are NOT stopped out: puts are allowed to assign (you get shares at a price
    you already accepted), calls are allowed to be called away (always at/above your cost).
  Sizing
  - Volatility-scaled: calmer ETFs get bigger allocations, jumpier ones smaller.
  - Caps per ticker, per group (stock-index ETFs together), and on total put obligations.
  - 10% cash always kept free. Kill switch at 15% below the 1-month equity high.
  Execution
  - Runs 3x a day. Limit orders only, starting at the mid price; unfilled orders are cancelled
    and re-priced a bit closer on the next run. Correct price increments per exchange rules.
  - Computes its own implied vol and delta (Black-Scholes) whenever the data feed omits them,
    so the free data plan can't silently stop it from trading.
  Reporting
  - Phone alert every run, alerts on assignments/called-away shares, errors flagged,
    and a Friday scorecard vs. SPY. State is saved in state.json in your repo.

LIMITS: can't see earnings dates (fine for ETFs; don't add single stocks without watching them),
doesn't know ex-dividend dates (an in-the-money call may be assigned a day early; harmless in the
wheel), and paper fills are more generous than real ones.

Not financial advice. You are responsible for every trade this places.
"""

import csv
import json
import math
import os
import re
import statistics
import sys
import traceback
import urllib.request
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from alpaca.data.enums import DataFeed, OptionsFeed
from alpaca.data.historical.option import OptionHistoricalDataClient
from alpaca.data.historical.stock import StockHistoricalDataClient
from alpaca.data.requests import OptionChainRequest, StockBarsRequest
from alpaca.data.timeframe import TimeFrame
from alpaca.trading.client import TradingClient
from alpaca.trading.enums import (AssetClass, ContractType, OrderSide, OrderStatus,
                                  PositionIntent, QueryOrderStatus, TimeInForce)
from alpaca.trading.requests import (GetOrdersRequest, GetPortfolioHistoryRequest,
                                     LimitOrderRequest)

ET = ZoneInfo("America/New_York")


def env_bool(name, default):
    v = os.getenv(name)
    return default if v is None or v == "" else v.strip().lower() in ("1", "true", "yes", "on")


def env_float(name, default):
    v = os.getenv(name)
    try:
        return float(v) if v not in (None, "") else default
    except ValueError:
        return default


def env_list(name, default):
    v = os.getenv(name)
    return default if not v else [s.strip().upper() for s in v.split(",") if s.strip()]


# ================================ SETTINGS ================================
PAPER = env_bool("PAPER", True)
DRY_RUN = env_bool("DRY_RUN", True)
SELL_PUTS = env_bool("SELL_PUTS", True)

# Liquid ETFs across stocks, small caps, gold, silver, bonds, banks, energy, emerging markets.
# The bot skips any it can't afford within the caps, so small accounts trade the cheaper ones.
WATCHLIST = env_list("WATCHLIST", ["SPY", "QQQ", "IWM", "GLD", "TLT", "XLF", "XLE", "EEM", "SLV"])
GROUPS = {
    "us_stock_index": {"SPY", "QQQ", "IWM", "DIA", "VOO", "IVV"},
    "precious_metals": {"GLD", "SLV", "IAU"},
}
MAX_GROUP_PCT = 0.50
MAX_PER_SYMBOL_PCT = env_float("MAX_PER_SYMBOL_PCT", 0.25)
MAX_TOTAL_PUT_PCT = 0.85        # all put obligations together, as a share of the account
CASH_RESERVE_PCT = 0.10
TARGET_VOL = 0.20               # volatility scaling: a 40%-vol ETF gets half the normal cap
MAX_DRAWDOWN_PCT = env_float("MAX_DRAWDOWN_PCT", 0.15)

MIN_DAYS, TARGET_DAYS, MAX_DAYS = 30, 45, 50
PUT_DELTA = (0.18, 0.25, 0.32)             # (min, target, max), absolute value
PUT_DELTA_DOWNTREND = (0.10, 0.16, 0.22)
CALL_DELTA = (0.20, 0.30, 0.40)
CALL_DELTA_REPAIR = (0.06, 0.15, 0.40)     # shares under water: strike must still be >= cost
MIN_PREMIUM = 0.15                         # per share
MAX_SPREAD_PCT, MAX_SPREAD_ABS = 0.30, 0.10  # reject if spread > both 30% of mid and $0.10
MIN_IV_RV_PUT, MIN_IV_RV_CALL = 1.05, 0.95
MIN_IV_RANK = 15                           # 0-100; only enforced once enough history exists
IV_HISTORY_MIN_DAYS = 20
CRASH_SIGMA = 2.5
TERM_STRUCTURE_LIMIT = 1.0                 # SPY 1-month IV / 3-month IV above this = stress

TAKE_PROFIT_PCT = 0.50
TIME_EXIT_DTE, TIME_EXIT_MIN_PROFIT = 21, 0.10

REPRICE_STEP, MAX_REPRICE = 0.25, 0.75     # share of half-spread conceded per retry
RISK_FREE = env_float("RISK_FREE", 0.04)
ORDER_TAG = "wb3"

OPTIONS_FEED = OptionsFeed.OPRA if os.getenv("OPTIONS_FEED", "").lower() == "opra" else OptionsFeed.INDICATIVE
NTFY_TOPIC = os.getenv("NTFY_TOPIC", "")
LOG_FILE, STATE_FILE = "trades_log.csv", "state.json"

PENNY_ALL = {"SPY", "QQQ", "IWM"}          # penny increments at every price
PENNY_PROGRAM = {"GLD", "TLT", "XLF", "XLE", "EEM", "SLV", "DIA", "SMH", "XLK", "XLV", "XLU",
                 "HYG", "KRE", "XBI", "GDX", "USO", "FXI", "EFA", "IAU", "TQQQ", "SOXL"}
# ==========================================================================

OCC = re.compile(r"^([A-Z.]{1,6})(\d{6})([CP])(\d{8})$")
messages, alerts = [], []


def say(msg):
    print(msg)
    messages.append(msg)


def alert(msg):
    say("⚠️ " + msg)
    alerts.append(msg)


def parse_occ(symbol):
    m = OCC.match(symbol or "")
    if not m:
        return None
    root, ymd, cp, strike = m.groups()
    return root, datetime.strptime(ymd, "%y%m%d").date(), cp, int(strike) / 1000.0


def underlying(symbol):
    info = parse_occ(symbol)
    return info[0] if info else symbol


# ------------------------------ pricing math ------------------------------
def _ncdf(x):
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def bs_price(cp, S, K, T, sigma, r=RISK_FREE):
    if T <= 0 or sigma <= 0:
        return max(0.0, (S - K) if cp == "C" else (K - S))
    d1 = (math.log(S / K) + (r + sigma * sigma / 2) * T) / (sigma * math.sqrt(T))
    d2 = d1 - sigma * math.sqrt(T)
    if cp == "C":
        return S * _ncdf(d1) - K * math.exp(-r * T) * _ncdf(d2)
    return K * math.exp(-r * T) * _ncdf(-d2) - S * _ncdf(-d1)


def bs_delta(cp, S, K, T, sigma, r=RISK_FREE):
    d1 = (math.log(S / K) + (r + sigma * sigma / 2) * T) / (sigma * math.sqrt(T))
    return _ncdf(d1) if cp == "C" else _ncdf(d1) - 1


def implied_vol(cp, price, S, K, T, r=RISK_FREE):
    """Bisection; returns None when the price is outside no-arbitrage bounds."""
    if price <= 0 or T <= 0:
        return None
    intrinsic = max(0.0, (S - K * math.exp(-r * T)) if cp == "C" else (K * math.exp(-r * T) - S))
    if price <= intrinsic + 1e-6:
        return None
    lo, hi = 0.005, 4.0
    if bs_price(cp, S, K, T, hi, r) < price:
        return None
    for _ in range(80):
        mid = (lo + hi) / 2
        if bs_price(cp, S, K, T, mid, r) > price:
            hi = mid
        else:
            lo = mid
    return (lo + hi) / 2


def tick_size(sym, price):
    if sym in PENNY_ALL:
        return 0.01
    if sym in PENNY_PROGRAM:
        return 0.01 if price < 3 else 0.05
    return 0.05 if price < 3 else 0.10


def price_for(sym, side, bid, ask, attempt):
    """Start at mid; each retry concedes part of the half-spread. Rounded to a legal increment."""
    mid = (bid + ask) / 2
    give = min(MAX_REPRICE, REPRICE_STEP * attempt) * (ask - bid) / 2
    p = mid - give if side == OrderSide.SELL else mid + give
    t = tick_size(sym, p)
    if side == OrderSide.SELL:
        return round(max(t, math.floor(p / t + 1e-9) * t), 2)
    return round(max(t, math.ceil(p / t - 1e-9) * t), 2)


# ------------------------------- state & io -------------------------------
def load_state():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except Exception:
        return {"iv_history": {}, "holdings": {}}


def save_state(state):
    try:
        with open(STATE_FILE, "w") as f:
            json.dump(state, f, indent=1, sort_keys=True)
    except Exception as e:
        print(f"(couldn't save state: {e})")


def log(action, symbol, qty, price, note=""):
    new = not os.path.exists(LOG_FILE)
    with open(LOG_FILE, "a", newline="") as f:
        w = csv.writer(f)
        if new:
            w.writerow(["timestamp_et", "mode", "action", "symbol", "qty", "price", "note"])
        mode = ("DRY" if DRY_RUN else "LIVE") + ("-paper" if PAPER else "-REAL")
        w.writerow([datetime.now(ET).isoformat(timespec="seconds"), mode, action, symbol, qty, f"{price:.2f}", note])


def notify():
    if not NTFY_TOPIC or not messages:
        return
    try:
        title = "Wheel bot" + (" (dry run)" if DRY_RUN else "") + (" - ATTENTION" if alerts else "")
        headers = {"Title": title}
        if alerts:
            headers["Priority"] = "high"
        req = urllib.request.Request(f"https://ntfy.sh/{NTFY_TOPIC}",
                                     data="\n".join(messages[-35:]).encode(), headers=headers)
        urllib.request.urlopen(req, timeout=10)
    except Exception as e:
        print(f"(notification failed: {e})")


# ------------------------------- market data -------------------------------
def stock_stats(sdata, symbols):
    out = {}
    bars = sdata.get_stock_bars(StockBarsRequest(
        symbol_or_symbols=symbols, timeframe=TimeFrame.Day, feed=DataFeed.IEX,
        start=datetime.now(timezone.utc) - timedelta(days=330)))
    for sym in symbols:
        closes = [b.close for b in bars.data.get(sym, []) if b.close]
        if len(closes) < 30:
            continue
        rets = [math.log(closes[i] / closes[i - 1]) for i in range(1, len(closes))]
        prior_sd = statistics.stdev(rets[-61:-1]) if len(rets) > 61 else statistics.stdev(rets[:-1])
        out[sym] = dict(
            price=closes[-1],
            rv=max(0.03, statistics.stdev(rets[-20:]) * math.sqrt(252)),
            sma200=(sum(closes[-200:]) / 200) if len(closes) >= 200 else None,
            move_sd=(rets[-1] / prior_sd) if prior_sd else 0.0,
        )
    return out


def enrich(snapshots, spot, today):
    """Turn raw snapshots into clean dicts with bid/ask/mid/iv/delta, computing IV & delta if missing."""
    rows = []
    for sym, snap in (snapshots or {}).items():
        info = parse_occ(sym)
        if not info:
            continue
        u, exp, cp, strike = info
        q = getattr(snap, "latest_quote", None)
        bid, ask = (getattr(q, "bid_price", 0) or 0), (getattr(q, "ask_price", 0) or 0)
        if bid <= 0 or ask <= 0 or ask < bid:
            continue
        mid = (bid + ask) / 2
        dte = (exp - today).days
        T = max(dte, 1) / 365.0
        iv = getattr(snap, "implied_volatility", None)
        g = getattr(snap, "greeks", None)
        delta = getattr(g, "delta", None) if g else None
        if not iv or iv <= 0:
            iv = implied_vol(cp, mid, spot, strike, T)
        if iv and delta is None:
            delta = bs_delta(cp, spot, strike, T, iv)
        if not iv or delta is None:
            continue
        rows.append(dict(symbol=sym, underlying=u, cp=cp, strike=strike, exp=exp, dte=dte,
                         bid=bid, ask=ask, mid=mid, iv=iv, delta=delta))
    return rows


def get_chain(odata, sym, ctype, today, dmin, dmax, **kw):
    try:
        return odata.get_option_chain(OptionChainRequest(
            underlying_symbol=sym, type=ctype, feed=OPTIONS_FEED,
            expiration_date_gte=today + timedelta(days=dmin),
            expiration_date_lte=today + timedelta(days=dmax), **kw))
    except Exception as e:
        say(f"({sym}: option chain unavailable: {e})")
        return {}


def atm_iv(odata, sym, spot, today, dmin, dmax):
    rows = []
    for ct in (ContractType.CALL, ContractType.PUT):
        rows += enrich(get_chain(odata, sym, ct, today, dmin, dmax,
                                 strike_price_gte=spot * 0.97, strike_price_lte=spot * 1.03), spot, today)
    if not rows:
        return None
    rows.sort(key=lambda r: abs(r["strike"] - spot))
    near = [r["iv"] for r in rows[:4]]
    return statistics.median(near) if near else None


def pick(rows, cp, spot, floor_strike, band, rv, min_iv_rv):
    lo, target, hi = band
    best = None
    for r in rows:
        if r["cp"] != cp or not (MIN_DAYS <= r["dte"] <= MAX_DAYS):
            continue
        if cp == "C" and r["strike"] < floor_strike:
            continue
        if cp == "P" and r["strike"] > spot:
            continue
        if not (lo <= abs(r["delta"]) <= hi):
            continue
        spread = r["ask"] - r["bid"]
        if r["mid"] < MIN_PREMIUM or (spread > MAX_SPREAD_ABS and spread / r["mid"] > MAX_SPREAD_PCT):
            continue
        if rv and min_iv_rv and r["iv"] / rv < min_iv_rv:
            continue
        score = abs(abs(r["delta"]) - target) * 10 + abs(r["dte"] - TARGET_DAYS) / 30 + spread / r["mid"]
        if best is None or score < best[0]:
            best = (score, r)
    return best[1] if best else None


# --------------------------------- orders ---------------------------------
def submit(trading, row_sym, under, qty, side, intent, limit, action, note):
    say(f"  -> {action.upper()} {qty}x {row_sym} @ ${limit:.2f} ({note})")
    log(action, row_sym, qty, limit, note)
    if DRY_RUN:
        return True
    for attempt in range(2):
        try:
            trading.submit_order(LimitOrderRequest(
                symbol=row_sym, qty=qty, side=side, time_in_force=TimeInForce.DAY,
                limit_price=limit, position_intent=intent,
                client_order_id=f"{ORDER_TAG}-{row_sym}-{datetime.now().strftime('%H%M%S%f')}"))
            return True
        except Exception as e:
            msg = str(e).lower()
            if attempt == 0 and ("increment" in msg or "tick" in msg or "sub-penny" in msg or "price" in msg):
                coarse = 0.05 if limit < 3 else 0.10
                limit = round((math.floor if side == OrderSide.SELL else math.ceil)(limit / coarse) * coarse, 2)
                limit = max(coarse, limit)
                continue
            alert(f"Order failed for {row_sym}: {e}")
            return False


def reset_orders(trading):
    """Cancel this bot's unfilled orders from earlier runs today; count retries per underlying."""
    start = datetime.combine(datetime.now(ET).date(), time(0, 0), tzinfo=ET)
    attempts = {}
    try:
        orders = trading.get_orders(GetOrdersRequest(status=QueryOrderStatus.ALL, after=start, limit=500))
    except Exception as e:
        say(f"(couldn't read orders: {e})")
        return attempts
    for o in orders:
        if not (o.client_order_id or "").startswith(ORDER_TAG):
            continue
        u = underlying(o.symbol)
        if o.status == OrderStatus.CANCELED:
            attempts[u] = attempts.get(u, 0) + 1
        elif o.status in (OrderStatus.NEW, OrderStatus.ACCEPTED, OrderStatus.PENDING_NEW,
                          OrderStatus.PARTIALLY_FILLED):
            attempts[u] = attempts.get(u, 0) + 1
            if not DRY_RUN:
                try:
                    trading.cancel_order_by_id(o.id)
                except Exception as e:
                    say(f"(couldn't cancel {o.symbol}: {e})")
    return attempts


# ---------------------------------- main ----------------------------------
def main():
    key, secret = os.getenv("ALPACA_API_KEY"), os.getenv("ALPACA_SECRET_KEY")
    if not key or not secret:
        sys.exit("Missing ALPACA_API_KEY / ALPACA_SECRET_KEY.")
    trading = TradingClient(key, secret, paper=PAPER)
    odata = OptionHistoricalDataClient(key, secret)
    sdata = StockHistoricalDataClient(key, secret)
    run(trading, odata, sdata)


def run(trading, odata, sdata):
    state = load_state()
    try:
        _run(trading, odata, sdata, state)
    finally:
        save_state(state)


def _run(trading, odata, sdata, state):
    now = datetime.now(ET)
    today = now.date()
    say(f"{'PAPER' if PAPER else 'REAL MONEY'} | {'DRY RUN' if DRY_RUN else 'LIVE ORDERS'} | {now:%a %b %d %I:%M %p} ET")

    if not trading.get_clock().is_open:
        say("Market closed. Nothing to do.")
        return
    if now.hour == 15 and now.minute >= 45:
        say("Too close to the close; skipping to avoid poor fills.")
        return

    attempts = reset_orders(trading)
    acct = trading.get_account()
    equity = float(acct.equity)
    cash_budget = float(acct.options_buying_power or acct.buying_power or 0) - equity * CASH_RESERVE_PCT

    positions = trading.get_all_positions()
    stocks = {p.symbol: p for p in positions if p.asset_class == AssetClass.US_EQUITY}
    shorts = [p for p in positions if p.asset_class == AssetClass.US_OPTION and float(p.qty) < 0]

    # ---- Detect assignments / called-away since last run ----
    prev = state.get("holdings", {})
    cur = {s: int(float(p.qty)) for s, p in stocks.items()}
    for s in set(prev) | set(cur):
        a, b = prev.get(s, 0), cur.get(s, 0)
        if b > a:
            say(f"📥 {s}: now own {b} shares (+{b - a}), likely put assignment.")
        elif b < a:
            say(f"📤 {s}: shares went from {a} to {b}, likely called away.")
    state["holdings"] = cur

    # ---- Kill switch ----
    halted = False
    try:
        h = trading.get_portfolio_history(GetPortfolioHistoryRequest(period="1M", timeframe="1D"))
        peak = max([e for e in (h.equity or []) if e] + [equity])
        dd = 1 - equity / peak
        if dd >= MAX_DRAWDOWN_PCT:
            halted = True
            alert(f"KILL SWITCH: account {dd:.0%} below its 1-month high. Only closing trades.")
    except Exception as e:
        say(f"(drawdown check failed: {e})")

    stats = stock_stats(sdata, sorted(set(WATCHLIST) | set(stocks) | {"SPY"}))

    # ---- Market regime (SPY) ----
    stress, reasons = False, []
    spy = stats.get("SPY")
    if spy:
        iv1 = atm_iv(odata, "SPY", spy["price"], today, 21, 40)
        iv3 = atm_iv(odata, "SPY", spy["price"], today, 75, 110)
        if iv1 and iv3:
            ratio = iv1 / iv3
            say(f"Market: SPY IV {iv1:.0%} (1m) vs {iv3:.0%} (3m), ratio {ratio:.2f}; realized {spy['rv']:.0%}")
            if ratio > TERM_STRUCTURE_LIMIT:
                stress = True
                reasons.append("inverted volatility curve")
        if spy["move_sd"] <= -CRASH_SIGMA:
            stress = True
            reasons.append(f"SPY down {spy['move_sd']:.1f} std devs today")
    if stress:
        alert("Market stress (" + ", ".join(reasons) + "). No new puts this run.")

    # ---- Exposure bookkeeping ----
    exposure, calls_sold, puts_open = {}, {}, set()
    put_obligation = 0.0
    for s, p in stocks.items():
        exposure[s] = exposure.get(s, 0) + abs(float(p.market_value))

    # ---- 1) Manage open short options ----
    for p in shorts:
        info = parse_occ(p.symbol)
        if not info:
            continue
        u, exp, cp, strike = info
        n = abs(int(float(p.qty)))
        if cp == "C":
            calls_sold[u] = calls_sold.get(u, 0) + n
        else:
            puts_open.add(u)
            exposure[u] = exposure.get(u, 0) + strike * 100 * n
            put_obligation += strike * 100 * n
        entry, mark = float(p.avg_entry_price), abs(float(p.current_price or 0))
        if entry <= 0 or mark <= 0:
            continue
        kept, dte = 1 - mark / entry, (exp - today).days
        status = f"{p.symbol}: kept {kept:+.0%}, {dte}d left"
        reason = None
        if kept >= TAKE_PROFIT_PCT:
            reason = f"hit {TAKE_PROFIT_PCT:.0%} target"
        elif dte <= TIME_EXIT_DTE and kept >= TIME_EXIT_MIN_PROFIT:
            reason = f"{dte}d left with {kept:.0%} banked"
        if not reason:
            say(status + (" (holding; assignment is fine)" if dte <= TIME_EXIT_DTE else ""))
            continue
        # Fresh quote for the exact contract if possible
        bid, ask = mark * 0.97, mark * 1.03
        try:
            snap = odata.get_option_chain(OptionChainRequest(
                underlying_symbol=u, feed=OPTIONS_FEED, type=ContractType.CALL if cp == "C" else ContractType.PUT,
                expiration_date=exp, strike_price_gte=strike - 0.01, strike_price_lte=strike + 0.01))
            q = getattr(snap.get(p.symbol), "latest_quote", None) if snap else None
            if q and q.bid_price and q.ask_price and q.ask_price >= q.bid_price > 0:
                bid, ask = q.bid_price, q.ask_price
        except Exception:
            pass
        limit = price_for(u, OrderSide.BUY, bid, ask, attempts.get(u, 0))
        say(status)
        if submit(trading, p.symbol, u, n, OrderSide.BUY, PositionIntent.BUY_TO_CLOSE, limit, "buy_to_close", reason):
            if cp == "C":
                calls_sold[u] -= n

    # ---- IV history (once per day per ticker) ----
    ivh = state.setdefault("iv_history", {})
    day_key = today.isoformat()

    def iv_rank(sym, current):
        hist = [v for d, v in sorted(ivh.get(sym, {}).items()) if d != day_key][-252:]
        if len(hist) < IV_HISTORY_MIN_DAYS or current is None:
            return None
        lo, hi = min(hist + [current]), max(hist + [current])
        return 100 * (current - lo) / (hi - lo) if hi > lo else 50

    def record_iv(sym, spot):
        if day_key in ivh.get(sym, {}):
            return ivh[sym][day_key]
        v = atm_iv(odata, sym, spot, today, 25, 55)
        if v:
            ivh.setdefault(sym, {})[day_key] = round(v, 4)
            # keep ~14 months
            for d in sorted(ivh[sym])[:-300]:
                del ivh[sym][d]
        return v

    if halted:
        return state

    def symbol_cap(sym, rv):
        scale = min(1.5, max(0.4, TARGET_VOL / rv)) if rv else 1.0
        return equity * MAX_PER_SYMBOL_PCT * scale

    def group_room(sym):
        for members in GROUPS.values():
            if sym in members:
                return equity * MAX_GROUP_PCT - sum(exposure.get(m, 0) for m in members)
        return float("inf")

    # ---- 2) Covered calls ----
    for sym, pos in stocks.items():
        if sym not in WATCHLIST:
            continue
        free = int(float(pos.qty) // 100) - calls_sold.get(sym, 0)
        if free <= 0:
            continue
        st = stats.get(sym, {})
        spot = float(pos.current_price)
        basis = float(pos.avg_entry_price)
        repair = spot < basis
        floor = max(spot, basis)
        rows = enrich(get_chain(odata, sym, ContractType.CALL, today, MIN_DAYS, MAX_DAYS,
                                strike_price_gte=floor), spot, today)
        c = pick(rows, "C", spot, floor, CALL_DELTA_REPAIR if repair else CALL_DELTA,
                 st.get("rv"), None if repair else MIN_IV_RV_CALL)
        if not c:
            say(f"{sym}: no call met the rules (price ${spot:.2f}, cost ${basis:.2f}"
                f"{', repair mode' if repair else ''}). Holding shares.")
            continue
        limit = price_for(sym, OrderSide.SELL, c["bid"], c["ask"], attempts.get(sym, 0))
        submit(trading, c["symbol"], sym, free, OrderSide.SELL, PositionIntent.SELL_TO_OPEN, limit, "sell_call",
               f"{c['dte']}d, delta {abs(c['delta']):.2f}, strike ${c['strike']:g}, IV {c['iv']:.0%}"
               + (", repair" if repair else ""))

    # ---- 3) Cash-secured puts ----
    if not SELL_PUTS or stress:
        return state
    for sym in WATCHLIST:
        if sym in puts_open or (sym in stocks and float(stocks[sym].qty) >= 100):
            continue
        st = stats.get(sym)
        if not st:
            say(f"{sym}: no price history.")
            continue
        spot = st["price"]
        if st["move_sd"] <= -CRASH_SIGMA:
            say(f"{sym}: big drop today ({st['move_sd']:.1f} std devs). Waiting.")
            continue
        room = min(symbol_cap(sym, st["rv"]) - exposure.get(sym, 0), group_room(sym),
                   cash_budget, equity * MAX_TOTAL_PUT_PCT - put_obligation)
        if room < spot * 100 * 0.85:
            say(f"{sym}: needs ~${spot * 100 * 0.9:,.0f} per contract; room ${max(room, 0):,.0f}. Skipping.")
            continue
        cur_iv = record_iv(sym, spot)
        ivr = iv_rank(sym, cur_iv)
        if ivr is not None and ivr < MIN_IV_RANK:
            say(f"{sym}: IV rank {ivr:.0f} is too low (premium is thin). Waiting.")
            continue
        downtrend = st["sma200"] is not None and spot < st["sma200"]
        rows = enrich(get_chain(odata, sym, ContractType.PUT, today, MIN_DAYS, MAX_DAYS,
                                strike_price_lte=spot, strike_price_gte=spot * 0.75), spot, today)
        p = pick(rows, "P", spot, 0, PUT_DELTA_DOWNTREND if downtrend else PUT_DELTA, st["rv"], MIN_IV_RV_PUT)
        if not p:
            say(f"{sym}: no put met the rules (RV {st['rv']:.0%}"
                f"{', IV ' + format(cur_iv, '.0%') if cur_iv else ''}{', downtrend' if downtrend else ''}).")
            continue
        n = int(room // (p["strike"] * 100))
        if n < 1:
            say(f"{sym}: best put needs ${p['strike'] * 100:,.0f}; room ${room:,.0f}. Skipping.")
            continue
        limit = price_for(sym, OrderSide.SELL, p["bid"], p["ask"], attempts.get(sym, 0))
        if submit(trading, p["symbol"], sym, n, OrderSide.SELL, PositionIntent.SELL_TO_OPEN, limit, "sell_put",
                  f"{p['dte']}d, delta {abs(p['delta']):.2f}, strike ${p['strike']:g}, IV {p['iv']:.0%} vs RV {st['rv']:.0%}"
                  + (f", IVR {ivr:.0f}" if ivr is not None else "") + (", downtrend" if downtrend else "")):
            cost = p["strike"] * 100 * n
            cash_budget -= cost
            put_obligation += cost
            exposure[sym] = exposure.get(sym, 0) + cost

    # Record IV for held/other tickers too so IV rank builds for everything
    for sym in WATCHLIST:
        if sym in stats and day_key not in ivh.get(sym, {}):
            record_iv(sym, stats[sym]["price"])

    # ---- Summary & Friday scorecard ----
    say(f"Account ${equity:,.0f} | put obligations ${put_obligation:,.0f} | cash budget left ${max(cash_budget, 0):,.0f}")
    if now.weekday() == 4 and now.hour >= 14:
        try:
            h = trading.get_portfolio_history(GetPortfolioHistoryRequest(period="1W", timeframe="1D"))
            eq = [e for e in (h.equity or []) if e]
            spy_bars = sdata.get_stock_bars(StockBarsRequest(symbol_or_symbols="SPY", timeframe=TimeFrame.Day,
                                                             feed=DataFeed.IEX,
                                                             start=datetime.now(timezone.utc) - timedelta(days=8)))
            sc = [b.close for b in spy_bars.data.get("SPY", [])]
            if len(eq) >= 2 and len(sc) >= 2:
                say(f"📊 WEEK: account {eq[-1] / eq[0] - 1:+.2%} vs SPY {sc[-1] / sc[0] - 1:+.2%}")
        except Exception as e:
            say(f"(weekly scorecard failed: {e})")
    return state


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception as e:
        alert(f"BOT ERROR: {e}")
        traceback.print_exc()
        raise
    finally:
        notify()
