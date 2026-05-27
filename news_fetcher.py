#!/usr/bin/env python3
"""
News headlines for screener picks and trade signals.

Source: CryptoPanic (cryptopanic.com/developers) — free API key, crypto-specific,
community-voted sentiment (bullish/bearish). Set CRYPTOPANIC_API_KEY in Railway.

All public functions return empty values silently on any error or missing key
so callers never need to guard against news failures.
"""

import os
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

CRYPTOPANIC_KEY = os.environ.get("CRYPTOPANIC_API_KEY", "")

# In-process cache: {symbol_upper: (fetch_timestamp, news_list)}
# Avoids re-fetching the same coin multiple times in one daily run.
_cache: dict = {}
_CACHE_TTL = 3600  # seconds


def fetch_coin_news(symbol_base: str, hours: int = 24, max_items: int = 3) -> list:
    """Return up to max_items recent headlines for symbol_base (e.g. 'INJ', 'BTC').

    Each item: {"title": str, "url": str, "sentiment": "bullish"|"bearish"|"neutral",
                "published_at": str (YYYY-MM-DD)}
    Returns [] on any error or missing API key.
    """
    if not CRYPTOPANIC_KEY:
        return []

    key = symbol_base.upper()
    now = time.monotonic()
    cached_ts, cached_news = _cache.get(key, (0, []))
    if now - cached_ts < _CACHE_TTL:
        return cached_news[:max_items]

    try:
        import requests
        r = requests.get(
            "https://cryptopanic.com/api/v1/posts/",
            params={
                "auth_token": CRYPTOPANIC_KEY,
                "currencies": key,
                "filter": "important",
                "public": "true",
            },
            timeout=8,
        )
        if r.status_code != 200:
            return []

        cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
        news = []
        for item in r.json().get("results", []):
            pub_raw = item.get("published_at", "")
            try:
                pub_dt = datetime.fromisoformat(pub_raw.replace("Z", "+00:00"))
                if pub_dt < cutoff:
                    continue
            except Exception:
                pass  # keep item if we can't parse the date

            votes = item.get("votes", {}) or {}
            pos = votes.get("positive", 0) or 0
            neg = votes.get("negative", 0) or 0
            if pos > neg * 1.5:
                sentiment = "bullish"
            elif neg > pos * 1.5:
                sentiment = "bearish"
            else:
                sentiment = "neutral"

            news.append({
                "title": (item.get("title") or "")[:120],
                "url": item.get("url", ""),
                "sentiment": sentiment,
                "published_at": pub_raw[:10],
            })

        _cache[key] = (now, news)
        return news[:max_items]

    except Exception:
        return []


def fetch_picks_news(symbols: list, hours: int = 24, max_per_coin: int = 2) -> dict:
    """Fetch news for a list of symbol bases.  Returns {symbol: [news_dicts]}.
    Symbols that return no news are omitted from the dict."""
    result = {}
    for sym in symbols:
        news = fetch_coin_news(sym, hours=hours, max_items=max_per_coin)
        if news:
            result[sym.upper()] = news
    return result


_SENTIMENT_EMOJI = {"bullish": "📈", "bearish": "📉", "neutral": "📰"}


def format_news_lines(news_list: list) -> list:
    """Return a list of formatted Discord markdown strings, one per headline."""
    lines = []
    for item in news_list:
        emoji = _SENTIMENT_EMOJI.get(item.get("sentiment", "neutral"), "📰")
        title = item.get("title", "")
        url = item.get("url", "")
        lines.append(f"{emoji} [{title}]({url})" if url else f"{emoji} {title}")
    return lines
