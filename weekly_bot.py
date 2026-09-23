"""
Weekly Bot - the same engine as wheel_bot.py, tuned for ~1-week options.

Runs in its OWN Alpaca paper account (separate API keys), so it never touches the
45-day wheel's positions. Keeps its own state (state_weekly.json) and log (trades_weekly.csv).

What's different from the 45-day wheel:
  - Sells options 4-12 days out (target ~8), so trades open and close every week.
  - Further out of the money (put delta ~0.16) because short-dated options react sharply
    to sudden moves.
  - Takes profit at 50%, and closes anything still profitable the day before expiration.
  - Sticks to ETFs with the most active weekly options.
Research note: weekly put-selling historically had smaller drawdowns than monthly, but
lower returns (Cboe WPUT vs PUT). This version is about faster feedback, not more money.

Not financial advice. You are responsible for every trade this places.
"""

import wheel_bot as wb

wb.WATCHLIST = wb.env_list("WATCHLIST", ["SPY", "QQQ", "IWM", "TLT", "GLD", "XLF", "SLV", "EEM"])
wb.MAX_PER_SYMBOL_PCT = wb.env_float("MAX_PER_SYMBOL_PCT", 0.30)

wb.MIN_DAYS, wb.TARGET_DAYS, wb.MAX_DAYS = 4, 8, 12
wb.PUT_DELTA = (0.10, 0.16, 0.22)
wb.PUT_DELTA_DOWNTREND = (0.06, 0.10, 0.14)
wb.CALL_DELTA = (0.15, 0.25, 0.35)
wb.CALL_DELTA_REPAIR = (0.04, 0.12, 0.35)
wb.MIN_PREMIUM = 0.05
wb.MAX_SPREAD_PCT, wb.MAX_SPREAD_ABS = 0.30, 0.05

wb.TAKE_PROFIT_PCT = 0.50
wb.TIME_EXIT_DTE, wb.TIME_EXIT_MIN_PROFIT = 1, 0.0   # close winners the day before expiry

wb.ORDER_TAG = "wk1"
wb.STATE_FILE, wb.LOG_FILE = "state_weekly.json", "trades_weekly.csv"

if __name__ == "__main__":
    try:
        wb.main()
    except SystemExit:
        raise
    except Exception as e:
        wb.alert(f"BOT ERROR: {e}")
        raise
    finally:
        wb.messages.insert(0, "📅 WEEKLY BOT")
        wb.notify()
