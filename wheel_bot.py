"""
Wheel Bot (Alpaca) - fully automated covered calls + cash-secured puts
=====================================================================

THE CYCLE ("the wheel"), repeated for every ticker in WATCHLIST:
  1. Don't own the shares?  Sell a cash-secured PUT below today's price and collect premium.
       - Expires worthless -> keep the premium, sell another put.
       - Stock falls below the strike -> you're assigned and buy 100 shares at the strike.
  2. Own 100+ shares?  Sell a covered CALL above today's price AND above your cost.
       - Expires worthless -> keep the premium, sell another call.
       - Stock rises above the strike -> shares are sold, back to step 1.
  3. Any option you sold that has kept 50%+ of its premium is bought back early,
     so the next run can sell a fresh one.
Assignment is handled automatically by the broker; the bot just reacts to what you hold.

SAFETY
  - PAPER=true and DRY_RUN=true by default (fake money, and print-only).
  - Never puts more than MAX_PER_SYMBOL_PCT of the account into one ticker.
  - Kill switch: stops opening new trades if the account falls MAX_DRAWDOWN_PCT
    below its 1-month high. Profit-taking buybacks keep running.
  - Only sells options when the market is open, with limit orders at the mid price.
  - Default watchlist is ETFs, which have no earnings reports (the biggest surprise risk
    for single stocks).

SETTINGS can be edited below or overridden with environment variables of the same name
(that's how the GitHub Actions schedule controls it).

Not financial advice. You are responsible for every trade this places.
"""

import csv
import json
import math
import os
import re
import sys
import urllib.request
from datetime import date, datetime, timedelta

from alpaca.data.enums import DataFeed, OptionsFeed
from alpaca.data.historical.option import OptionHistoricalDataClient
from alpaca.data.historical.stock import StockHistoricalDataClient
from alpaca.data.requests import OptionChainRequest, StockLatestTradeRequest
from alpaca.trading.client import TradingClient
from alpaca.trading.enums import (AssetClass, ContractType, OrderSide, PositionIntent,
                                  QueryOrderStatus, TimeInForce)
from alpaca.trading.requests import (GetOrdersRequest, GetPortfolioHistoryRequest,
                                     LimitOrderRequest)


def env_bool(name, default):
    v = os.getenv(name)
    return default if v is None or v == "" else v.strip().lower() in ("1", "true", "yes", "on")


def env_list(name, default):
    v = os.getenv(name)
    return default if not v else [s.strip().upper() for s in v.split(",") if s.strip()]


# ------------------------------ SETTINGS ------------------------------
PAPER = env_bool("PAPER", True)        # fake money
DRY_RUN = env_bool("DRY_RUN", True)    # print only, no orders

WATCHLIST = env_list("WATCHLIST", ["SPY", "QQQ", "IWM", "XLF", "SCHD"])
SELL_PUTS = env_bool("SELL_PUTS", True)  # False = covered calls only, never buy new shares

MIN_DAYS, MAX_DAYS = 30, 45
CALL_DELTA = (0.15, 0.30, 0.40)        # (min, target, max)
PUT_DELTA = (0.15, 0.25, 0.35)         # absolute value of put delta
MIN_PREMIUM = 0.20                     # per share ($20 per contract)
MAX_SPREAD_PCT = 0.25                  # skip illiquid options
TAKE_PROFIT_PCT = 0.50                 # buy back after keeping 50% of premium
MAX_PER_SYMBOL_PCT = float(os.getenv("MAX_PER_SYMBOL_PCT", 0.25))
MAX_DRAWDOWN_PCT = float(os.getenv("MAX_DRAWDOWN_PCT", 0.15))
CASH_RESERVE_PCT = 0.10                # always keep 10% of the account unused
OPTIONS_FEED = OptionsFeed.INDICATIVE  # free; switch to OPRA if you pay for it
NTFY_TOPIC = os.getenv("NTFY_TOPIC", "")  # optional phone alerts via the free ntfy app
LOG_FILE = "trades_log.csv"
# ----------------------------------------------------------------------

OCC = re.compile(r"^([A-Z.]{1,6})(\d{6})([CP])(\d{8})$")
messages = []


def say(msg):
    print(msg)
    messages.append(msg)


def parse_occ(symbol):
    m = OCC.match(symbol)
    if not m:
        return None
    root, ymd, cp, strike = m.groups()
    return root, datetime.strptime(ymd, "%y%m%d").date(), cp, int(strike) / 1000.0


def round_price(p):
    tick = 0.05 if p >= 3 else 0.01
    return max(tick, math.floor(p / tick + 1e-9) * tick)


def log(action, symbol, qty, price, note=""):
    new = not os.path.exists(LOG_FILE)
    with open(LOG_FILE, "a", newline="") as f:
        w = csv.writer(f)
        if new:
            w.writerow(["timestamp", "mode", "action", "symbol", "qty", "price", "note"])
        mode = ("DRY" if DRY_RUN else "LIVE") + ("-paper" if PAPER else "-REAL")
        w.writerow([datetime.now().isoformat(timespec="seconds"), mode, action, symbol, qty, f"{price:.2f}", note])


def notify():
    if not NTFY_TOPIC or not messages:
        return
    try:
        body = "\n".join(messages[-25:]).encode()
        req = urllib.request.Request(f"https://ntfy.sh/{NTFY_TOPIC}", data=body,
                                     headers={"Title": "Wheel bot" + (" (dry run)" if DRY_RUN else "")})
        urllib.request.urlopen(req, timeout=10)
    except Exception as e:
        print(f"(notification failed: {e})")


def pick(snapshots, cp, price, floor_strike, today):
    """Best contract by delta closeness. cp='C' needs strike >= floor_strike; cp='P' needs strike <= price."""
    lo, target, hi = CALL_DELTA if cp == "C" else PUT_DELTA
    best = None
    for sym, snap in snapshots.items():
        info = parse_occ(sym)
        if not info or info[2] != cp:
            continue
        _, exp, _, strike = info
        dte = (exp - today).days
        if not (MIN_DAYS <= dte <= MAX_DAYS):
            continue
        if cp == "C" and strike < floor_strike:
            continue
        if cp == "P" and strike > price:
            continue
        d = snap.greeks.delta if snap.greeks and snap.greeks.delta is not None else None
        if d is None or not (lo <= abs(d) <= hi):
            continue
        q = snap.latest_quote
        if not q or not q.bid_price or not q.ask_price or q.bid_price <= 0:
            continue
        mid = (q.bid_price + q.ask_price) / 2
        if mid < MIN_PREMIUM or (q.ask_price - q.bid_price) / mid > MAX_SPREAD_PCT:
            continue
        score = abs(abs(d) - target)
        if best is None or score < best[0]:
            best = (score, dict(symbol=sym, strike=strike, exp=exp, dte=dte, delta=d,
                                bid=q.bid_price, ask=q.ask_price, mid=mid))
    return best[1] if best else None


def submit(trading, symbol, qty, side, intent, limit, action, note):
    log(action, symbol, qty, limit, note)
    if DRY_RUN:
        return
    try:
        trading.submit_order(LimitOrderRequest(symbol=symbol, qty=qty, side=side,
                                               time_in_force=TimeInForce.DAY,
                                               limit_price=round(limit, 2), position_intent=intent))
    except Exception as e:
        say(f"  ORDER FAILED for {symbol}: {e}")


def main():
    key, secret = os.getenv("ALPACA_API_KEY"), os.getenv("ALPACA_SECRET_KEY")
    if not key or not secret:
        sys.exit("Missing ALPACA_API_KEY / ALPACA_SECRET_KEY.")

    trading = TradingClient(key, secret, paper=PAPER)
    odata = OptionHistoricalDataClient(key, secret)
    sdata = StockHistoricalDataClient(key, secret)
    say(f"Mode: {'PAPER' if PAPER else 'REAL MONEY'}, {'DRY RUN' if DRY_RUN else 'placing orders'}")

    if not trading.get_clock().is_open:
        say("Market closed. Nothing to do.")
        return

    today = date.today()
    acct = trading.get_account()
    equity = float(acct.equity)
    opt_bp = float(acct.options_buying_power or acct.buying_power)

    # ---- Kill switch ----
    halted = False
    try:
        hist = trading.get_portfolio_history(GetPortfolioHistoryRequest(period="1M", timeframe="1D"))
        peak = max([e for e in (hist.equity or []) if e] + [equity])
        dd = 1 - equity / peak if peak else 0
        if dd >= MAX_DRAWDOWN_PCT:
            halted = True
            say(f"KILL SWITCH: account is {dd:.0%} below its 1-month high. No new trades.")
    except Exception as e:
        say(f"(couldn't check drawdown: {e})")

    positions = trading.get_all_positions()
    stocks = {p.symbol: p for p in positions if p.asset_class == AssetClass.US_EQUITY}
    shorts = [p for p in positions if p.asset_class == AssetClass.US_OPTION and float(p.qty) < 0]
    pending = [o.symbol for o in trading.get_orders(GetOrdersRequest(status=QueryOrderStatus.OPEN))]
    pending_under = {(parse_occ(s) or ("",))[0] for s in pending}

    calls_sold, put_collateral = {}, {}

    # ---- 1) Take profit on anything already sold ----
    for p in shorts:
        info = parse_occ(p.symbol)
        if not info:
            continue
        under, _, cp, strike = info
        n = abs(int(float(p.qty)))
        if cp == "C":
            calls_sold[under] = calls_sold.get(under, 0) + n
        else:
            put_collateral[under] = put_collateral.get(under, 0) + strike * 100 * n
        entry, now = float(p.avg_entry_price), abs(float(p.current_price or 0))
        if p.symbol in pending or entry <= 0 or now <= 0:
            continue
        kept = 1 - now / entry
        if kept >= TAKE_PROFIT_PCT:
            limit = now * 1.02 + 0.01
            say(f"{p.symbol}: kept {kept:.0%} -> BUY TO CLOSE {n}x at ${limit:.2f}")
            submit(trading, p.symbol, n, OrderSide.BUY, PositionIntent.BUY_TO_CLOSE, limit,
                   "buy_to_close", f"kept {kept:.0%}")

    if halted:
        return

    # ---- 2) Covered calls on shares you hold ----
    for sym, pos in stocks.items():
        if sym not in WATCHLIST or sym in pending_under:
            continue
        free = int(float(pos.qty) // 100) - calls_sold.get(sym, 0)
        if free <= 0:
            continue
        price, basis = float(pos.current_price), float(pos.avg_entry_price)
        chain = odata.get_option_chain(OptionChainRequest(
            underlying_symbol=sym, type=ContractType.CALL, feed=OPTIONS_FEED,
            expiration_date_gte=today + timedelta(days=MIN_DAYS),
            expiration_date_lte=today + timedelta(days=MAX_DAYS),
            strike_price_gte=max(price, basis)))
        c = pick(chain, "C", price, max(price, basis), today)
        if not c:
            say(f"{sym}: no call met the rules today.")
            continue
        limit = round_price(c["mid"])
        say(f"{sym}: SELL CALL {free}x {c['symbol']} strike ${c['strike']:.2f}, {c['dte']}d, "
            f"delta {c['delta']:.2f}, ${limit:.2f}/sh = ${limit*100*free:.0f}")
        submit(trading, c["symbol"], free, OrderSide.SELL, PositionIntent.SELL_TO_OPEN, limit,
               "sell_call", f"delta {c['delta']:.2f}")

    # ---- 3) Cash-secured puts on tickers you don't hold ----
    if not SELL_PUTS:
        return
    budget_total = opt_bp - equity * CASH_RESERVE_PCT
    for sym in WATCHLIST:
        if sym in pending_under or sym in put_collateral:
            continue
        held = stocks.get(sym)
        if held and float(held.qty) >= 100:
            continue  # already in the covered-call half of the wheel
        price = float(sdata.get_stock_latest_trade(
            StockLatestTradeRequest(symbol_or_symbols=sym, feed=DataFeed.IEX))[sym].price)
        exposure = (float(held.market_value) if held else 0)
        room = min(equity * MAX_PER_SYMBOL_PCT - exposure, budget_total)
        if room < price * 100 * 0.8:
            say(f"{sym}: not enough room for 100 shares (${price*100:,.0f}); skipping puts.")
            continue
        chain = odata.get_option_chain(OptionChainRequest(
            underlying_symbol=sym, type=ContractType.PUT, feed=OPTIONS_FEED,
            expiration_date_gte=today + timedelta(days=MIN_DAYS),
            expiration_date_lte=today + timedelta(days=MAX_DAYS),
            strike_price_lte=price))
        p = pick(chain, "P", price, 0, today)
        if not p:
            say(f"{sym}: no put met the rules today.")
            continue
        n = int(room // (p["strike"] * 100))
        if n < 1:
            continue
        limit = round_price(p["mid"])
        say(f"{sym}: SELL PUT {n}x {p['symbol']} strike ${p['strike']:.2f}, {p['dte']}d, "
            f"delta {p['delta']:.2f}, ${limit:.2f}/sh = ${limit*100*n:.0f} "
            f"(sets aside ${p['strike']*100*n:,.0f})")
        submit(trading, p["symbol"], n, OrderSide.SELL, PositionIntent.SELL_TO_OPEN, limit,
               "sell_put", f"delta {p['delta']:.2f}")
        budget_total -= p["strike"] * 100 * n


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        say(f"BOT ERROR: {e}")
        raise
    finally:
        if DRY_RUN:
            print("\nDRY RUN: no orders placed.")
        notify()
