#!/usr/bin/env python3
"""
TradingView integration: export picks as watchlist (.txt), generate Pine Script
score lookup snippets, map CoinGecko IDs to TradingView symbols.

Usage:
    python tv_integration.py watchlist --exchange BINANCE
    python tv_integration.py pine --exchange BINANCE
"""

import argparse
import json
import os
import sqlite3
import time
from pathlib import Path

import requests

DB_PATH = Path(os.environ.get("CRYPTO_AGENT_DB", "crypto_agent.db"))

EXCHANGE_API = {
    "BINANCE":    "https://api.binance.com",
    "BINANCE_US": "https://api.binance.us",
    "BYBIT":      "https://api.bybit.com",
}

_tradeable_cache: dict[str, set] = {}


def _get_coinbase_pairs() -> set:
    """Fetch all base assets tradeable on Coinbase (USD or USDC quoted).
    Uses the public Coinbase Exchange API — no key needed.
    """
    key = "COINBASE:USD"
    if key in _tradeable_cache:
        return _tradeable_cache[key]
    try:
        r = requests.get("https://api.exchange.coinbase.com/products", timeout=10)
        r.raise_for_status()
        tradeable = {
            p["base_currency"].upper()
            for p in r.json()
            if p.get("quote_currency") in ("USD", "USDC")
            and p.get("status") == "online"
            and not p.get("trading_disabled", True)
        }
        _tradeable_cache[key] = tradeable
        return tradeable
    except Exception as e:
        print(f"Warning: could not fetch Coinbase pairs ({e})")
        return set()


def get_tradeable_pairs(exchange: str, quote: str = "USDT") -> set:
    """Fetch base symbols tradeable on the given exchange.

    Special value 'US_EXCHANGES' returns the union of Binance.US and Coinbase —
    a coin passes if it's available on either platform.
    """
    if exchange == "US_EXCHANGES":
        binance_us = get_tradeable_pairs("BINANCE_US", quote)
        coinbase   = _get_coinbase_pairs()
        combined   = binance_us | coinbase
        _tradeable_cache["US_EXCHANGES:USD"] = combined
        return combined

    key = f"{exchange}:{quote}"
    if key in _tradeable_cache:
        return _tradeable_cache[key]

    base_url = EXCHANGE_API.get(exchange, "")
    if not base_url:
        return set()

    try:
        r = requests.get(f"{base_url}/api/v3/exchangeInfo", timeout=10)
        r.raise_for_status()
        tradeable = {
            s["baseAsset"].upper()
            for s in r.json().get("symbols", [])
            if s.get("quoteAsset", "").upper() == quote.upper()
            and s.get("status") == "TRADING"
        }
        _tradeable_cache[key] = tradeable
        return tradeable
    except Exception as e:
        print(f"Warning: could not fetch {exchange} pairs ({e}) — skipping filter")
        return set()

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


def export_watchlist(date=None, exchange="BINANCE", out_path=None, filter_exchange=None):
    # Warn if the picks in the local DB are stale
    if date is None:
        from datetime import datetime, timezone, timedelta
        today = datetime.now(timezone.utc).date()
        conn = sqlite3.connect(DB_PATH)
        cur = conn.cursor()
        cur.execute("SELECT MAX(pick_date) FROM picks")
        row = cur.fetchone()
        conn.close()
        latest = row[0] if row else None
        if latest:
            pick_date = datetime.strptime(latest, "%Y-%m-%d").date()
            age = (today - pick_date).days
            if age >= 1:
                print(f"⚠  Local picks are {age} day(s) old ({latest}). "
                      f"Run 'python3 crypto_agent.py fetch' first for today's picks.")
    """Write a TradingView-importable watchlist (one symbol per line).

    filter_exchange: if set, pre-flight checks the exchange's public API and
    drops picks not listed there. Pass 'BINANCE_US' for Binance.US users.
    """
    date, rows = latest_picks(date)
    if not rows:
        print(f"No picks found for {date}")
        return

    dropped = []
    if filter_exchange:
        quote = EXCHANGE_QUOTE.get(exchange, "USDT")
        tradeable = get_tradeable_pairs(filter_exchange, quote)
        if tradeable:
            kept = []
            for row in rows:
                coin_id, sym, score, price = row
                base = SYMBOL_OVERRIDES.get(coin_id, sym).upper()
                if base in tradeable:
                    kept.append(row)
                else:
                    dropped.append(sym)
            rows = kept

    out_path = out_path or Path(f"watchlist_{date}_{exchange.lower()}.txt")
    with open(out_path, "w") as f:
        f.write(f"###Top picks {date}\n")
        for coin_id, sym, _, _ in rows:
            f.write(coingecko_to_tv_symbol(coin_id, sym, exchange) + "\n")

    print(f"Wrote {len(rows)} symbols to {out_path}")
    if dropped:
        print(f"Dropped {len(dropped)} picks not listed on {filter_exchange}: {', '.join(dropped)}")
    print("In TradingView: Watchlist → Import list → select this file")


def get_pine_score_string(date=None, exchange="BINANCE", filter_exchange=None) -> str:
    """Return the get_score() Pine Script block as a string for Discord posting."""
    date, rows = latest_picks(date)
    if not rows:
        return ""

    if filter_exchange:
        quote = EXCHANGE_QUOTE.get(exchange, "USDT")
        tradeable = get_tradeable_pairs(filter_exchange, quote)
        if tradeable:
            rows = [r for r in rows
                    if SYMBOL_OVERRIDES.get(r[0], r[1]).upper() in tradeable]

    lines = [
        f"get_score(ticker) =>",
        f"    s = 0.0",
    ]
    for coin_id, sym, score, _ in rows:
        tv_ticker = coingecko_to_tv_symbol(coin_id, sym, exchange).split(":")[1]
        lines.append(f"    if ticker == \"{tv_ticker}\"")
        lines.append(f"        s := {score:.2f}")
    lines.append("    s")
    return "\n".join(lines)


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
    p.add_argument("--filter-exchange", default=None,
                   help="Pre-flight filter: drop picks not listed on this exchange. "
                        "Use BINANCE_US, COINBASE, or US_EXCHANGES (Binance.US + Coinbase combined).")
    p.add_argument("--date", default=None, help="YYYY-MM-DD (default: latest)")
    args = p.parse_args()
    if args.command == "watchlist":
        export_watchlist(date=args.date, exchange=args.exchange,
                         filter_exchange=args.filter_exchange)
    else:
        export_pine_score_block(date=args.date, exchange=args.exchange)


if __name__ == "__main__":
    main()
