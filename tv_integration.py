#!/usr/bin/env python3
"""
TradingView integration: export picks as watchlist (.txt), generate Pine Script
score lookup snippets, map CoinGecko IDs to TradingView symbols.

Usage:
    python tv_integration.py watchlist --exchange BINANCE
    python tv_integration.py pine --exchange BINANCE
"""

import argparse
import sqlite3
from pathlib import Path

DB_PATH = Path("crypto_agent.db")

# CoinGecko symbol → exchange ticker overrides
SYMBOL_OVERRIDES = {
    # coin_id : ticker_base
    # add as you encounter mismatches
}

EXCHANGE_QUOTE = {
    "BINANCE":  "USDT",
    "COINBASE": "USD",
    "KRAKEN":   "USD",
    "BYBIT":    "USDT",
    "OKX":      "USDT",
}

# Bitcoin = XBT on Kraken, etc.
EXCHANGE_BASE_OVERRIDES = {
    "KRAKEN": {"BTC": "XBT", "DOGE": "XDG"},
}


def coingecko_to_tv_symbol(coin_id, symbol, exchange="BINANCE"):
    """Build a TradingView symbol like 'BINANCE:BTCUSDT'."""
    base = SYMBOL_OVERRIDES.get(coin_id, symbol).upper()
    base = EXCHANGE_BASE_OVERRIDES.get(exchange, {}).get(base, base)
    quote = EXCHANGE_QUOTE.get(exchange, "USDT")
    return f"{exchange}:{base}{quote}"


def latest_picks(date=None):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    if not date:
        cur.execute("SELECT MAX(pick_date) FROM picks")
        date = cur.fetchone()[0]
    cur.execute(
        "SELECT coin_id, symbol, composite_score, entry_price "
        "FROM picks WHERE pick_date = ? ORDER BY rank",
        (date,)
    )
    rows = cur.fetchall()
    conn.close()
    return date, rows


def export_watchlist(date=None, exchange="BINANCE", out_path=None):
    """Write a TradingView-importable watchlist (one symbol per line)."""
    date, rows = latest_picks(date)
    if not rows:
        print(f"No picks found for {date}")
        return
    out_path = out_path or Path(f"watchlist_{date}_{exchange.lower()}.txt")
    with open(out_path, "w") as f:
        f.write(f"###Top picks {date}\n")
        for coin_id, sym, _, _ in rows:
            f.write(coingecko_to_tv_symbol(coin_id, sym, exchange) + "\n")
    print(f"Wrote {len(rows)} symbols to {out_path}")
    print("In TradingView: Watchlist → Import list → select this file")


def export_pine_score_block(date=None, exchange="BINANCE", out_path=None):
    """Generate a Pine Script lookup function: ticker → screener score.
    Paste the output into the indicator under the score input."""
    date, rows = latest_picks(date)
    if not rows:
        print(f"No picks found for {date}")
        return
    out_path = out_path or Path(f"scores_{date}.pine")
    lines = [
        f"// Screener scores for {date}",
        "// Paste this function into screener_confirmation.pine, then call:",
        "//   score := get_score(syminfo.ticker)",
        "get_score(ticker) =>",
        "    s = 0.0",
    ]
    for coin_id, sym, score, _ in rows:
        tv_full = coingecko_to_tv_symbol(coin_id, sym, exchange)
        tv_ticker = tv_full.split(":")[1]
        lines.append(f"    if ticker == \"{tv_ticker}\"")
        lines.append(f"        s := {score:.4f}")
    lines.append("    s")
    with open(out_path, "w") as f:
        f.write("\n".join(lines) + "\n")
    print(f"Wrote Pine Script snippet to {out_path}")


def main():
    p = argparse.ArgumentParser(description="TradingView export utilities")
    p.add_argument("command", choices=["watchlist", "pine"])
    p.add_argument("--exchange", default="BINANCE", choices=list(EXCHANGE_QUOTE.keys()))
    p.add_argument("--date", default=None, help="YYYY-MM-DD (default: latest)")
    args = p.parse_args()
    if args.command == "watchlist":
        export_watchlist(date=args.date, exchange=args.exchange)
    else:
        export_pine_score_block(date=args.date, exchange=args.exchange)


if __name__ == "__main__":
    main()
