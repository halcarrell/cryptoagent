#!/usr/bin/env python3
"""
News headlines for screener picks and trade signals.

Primary source: Tiingo news API (api.tiingo.com) — ticker-specific, free tier.
  Set TIINGO_API_KEY in Railway (already used for price data).
Fallback: CoinDesk + CoinTelegraph + Decrypt RSS feeds — no key needed.

Sentiment is inferred from headline keywords (bullish/bearish/neutral).
All public functions return empty values silently on any error.
"""

import os
import re
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

TIINGO_KEY = os.environ.get("TIINGO_API_KEY", "")

_RSS_FEEDS = [
    "https://feeds.feedburner.com/CoinDesk",
    "https://cointelegraph.com/rss",
    "https://decrypt.co/feed",
]

# Project names for tickers where the symbol alone is ambiguous or short.
_COIN_NAMES: dict = {
    "BTC":   ["bitcoin"],
    "ETH":   ["ethereum"],
    "BNB":   ["bnb", "binance coin", "binance smart chain"],
    "SOL":   ["solana"],
    "LTC":   ["litecoin"],
    "BCH":   ["bitcoin cash"],
    "XLM":   ["stellar", "stellar lumens"],
    "TRX":   ["tron"],
    "ETC":   ["ethereum classic"],
    "INJ":   ["injective"],
    "TIA":   ["celestia"],
    "AKT":   ["akash"],
    "ATOM":  ["cosmos"],
    "AVAX":  ["avalanche"],
    "LINK":  ["chainlink"],
    "DOT":   ["polkadot"],
    "MATIC": ["polygon"],
    "OP":    ["optimism"],
    "ARB":   ["arbitrum"],
    "APT":   ["aptos"],
    "SEI":   ["sei network"],
    "DOGE":  ["dogecoin"],
    "ADA":   ["cardano"],
    "XRP":   ["ripple"],
    "NEAR":  ["near protocol"],
    "FTM":   ["fantom"],
    "HBAR":  ["hedera"],
    "ICP":   ["internet computer"],
    "FIL":   ["filecoin"],
    "GRT":   ["the graph"],
    "LDO":   ["lido"],
    "UNI":   ["uniswap"],
    "RUNE":  ["thorchain"],
    "RNDR":  ["render network"],
    "WLD":   ["worldcoin"],
    "TAO":   ["bittensor"],
    "STX":   ["stacks"],
    "PEPE":  ["pepe"],
    "BONK":  ["bonk"],
    "WIF":   ["dogwifhat", "wif hat"],
    "TON":   ["toncoin", "the open network"],
    "FLOKI": ["floki"],
    "POL":   ["polygon", "pol"],
    "NOT":   ["notcoin"],
    "POPCAT":["popcat"],
    "TRUMP": ["official trump"],
    "KAS":   ["kaspa"],
    "IMX":   ["immutable", "immutable x"],
    "SNX":   ["synthetix"],
    "AAVE":  ["aave"],
    "MKR":   ["maker", "makerdao"],
    "CRV":   ["curve", "curve finance"],
    "ENS":   ["ethereum name service"],
    "SUSHI": ["sushiswap"],
    "VET":   ["vechain"],
    "ZIL":   ["zilliqa"],
    "MANA":  ["decentraland"],
    "SAND":  ["sandbox", "the sandbox"],
    "GALA":  ["gala games"],
    "BLUR":  ["blur"],
    "APE":   ["apecoin"],
    "CHZ":   ["chiliz"],
    "PYTH":  ["pyth network"],
    "JTO":   ["jito"],
    "STRK":  ["starknet"],
    "ONDO":  ["ondo finance"],
    "JUP":   ["jupiter"],
    "ENA":   ["ethena"],
    "W":     ["wormhole"],
    "ZK":    ["zksync", "zk sync"],
    "ZRO":   ["layerzero"],
    "EIGEN": ["eigenlayer"],
    "DYM":   ["dymension"],
    "MEME":  ["memecoin"],
}

_BULLISH_RE = re.compile(
    r"\b(?:surge[sd]?|surging|soar[sd]?|rally|rallied|rallying|"
    r"breakout|bullish|rise[sd]?|rising|jump[sed]?|gain[sed]?|"
    r"breakthrough|milestone|partnership[s]?|upgrade[sd]?|launch(?:ed|es)?)\b",
    re.IGNORECASE,
)
_BEARISH_RE = re.compile(
    r"\b(?:crash(?:ed|es)?|drop[sed]?|fall[sn]?|fell|bearish|decline[sd]?|"
    r"collapse[sd]?|correction[s]?|dump[sed]?|sell.?off|plunge[sd]?|"
    r"outflow[s]?|hack(?:ed|s)?|exploit(?:ed|s)?|ban(?:ned|s)?|"
    r"lawsuit[s]?|fine[sd]?|warning[s]?|concern[s]?|shed[s]?)\b",
    re.IGNORECASE,
)

# Caches
_coin_cache: dict = {}          # {ticker:hours: (ts, list)}
_feed_cache: dict = {}          # {url: (ts, items)}
_COIN_CACHE_TTL = 3600
_FEED_CACHE_TTL = 900


def _sentiment(text: str) -> str:
    bulls = len(_BULLISH_RE.findall(text))
    bears = len(_BEARISH_RE.findall(text))
    if bulls > bears:
        return "bullish"
    if bears > bulls:
        return "bearish"
    return "neutral"


# ── Tiingo ────────────────────────────────────────────────────────────────────

def _fetch_tiingo(ticker: str, hours: int, max_items: int) -> list:
    """Fetch ticker-specific news from Tiingo. Returns [] on any failure."""
    if not TIINGO_KEY:
        return []
    try:
        import requests
        start_date = (datetime.now(timezone.utc) - timedelta(hours=hours)).strftime("%Y-%m-%d")
        r = requests.get(
            "https://api.tiingo.com/tiingo/news",
            params={
                "tickers": ticker,
                "startDate": start_date,
                "limit": max_items,
                "token": TIINGO_KEY,
            },
            timeout=8,
        )
        if r.status_code != 200:
            return []
        results = []
        for item in r.json()[:max_items]:
            title = (item.get("title") or "")[:120]
            desc  = item.get("description") or ""
            results.append({
                "title":        title,
                "url":          item.get("url", ""),
                "sentiment":    _sentiment(title + " " + desc),
                "published_at": (item.get("publishedDate") or "")[:10],
            })
        return results
    except Exception:
        return []


# ── RSS fallback ──────────────────────────────────────────────────────────────

def _parse_feed(url: str) -> list:
    """Fetch and cache one RSS feed for up to 15 minutes."""
    now = time.monotonic()
    ts, items = _feed_cache.get(url, (0, []))
    if now - ts < _FEED_CACHE_TTL:
        return items
    try:
        import requests
        r = requests.get(url, timeout=8, headers={"User-Agent": "CryptoScreener/1.0"})
        if r.status_code != 200:
            return []
        root   = ET.fromstring(r.content)
        parsed = []
        for item in root.findall("./channel/item"):
            title = item.findtext("title") or ""
            link  = item.findtext("link")  or ""
            pub   = item.findtext("pubDate") or ""
            try:
                pub_dt = parsedate_to_datetime(pub)
            except Exception:
                pub_dt = datetime.now(timezone.utc)
            parsed.append({"title": title, "link": link, "published_at": pub_dt})
        _feed_cache[url] = (now, parsed)
        return parsed
    except Exception:
        return []


def _rss_matches(title: str, ticker: str) -> bool:
    """True if the headline mentions this coin by ticker or known project name."""
    if re.search(r"\b" + re.escape(ticker) + r"\b", title, re.IGNORECASE):
        return True
    for name in _COIN_NAMES.get(ticker.upper(), []):
        if name.lower() in title.lower():
            return True
    return False


def _fetch_rss(ticker: str, hours: int, max_items: int) -> list:
    """Filter RSS feeds for headlines mentioning ticker. Returns []  on failure."""
    cutoff  = datetime.now(timezone.utc) - timedelta(hours=hours)
    results = []
    seen    = set()
    for url in _RSS_FEEDS:
        for item in _parse_feed(url):
            if item["published_at"] < cutoff:
                continue
            title = item["title"]
            if title in seen or not _rss_matches(title, ticker):
                continue
            seen.add(title)
            results.append({
                "title":        title[:120],
                "url":          item["link"],
                "sentiment":    _sentiment(title),
                "published_at": item["published_at"].strftime("%Y-%m-%d"),
            })
    results.sort(key=lambda x: x["published_at"], reverse=True)
    return results[:max_items]


# ── Public API ────────────────────────────────────────────────────────────────

def fetch_coin_news(symbol_base: str, hours: int = 24, max_items: int = 3) -> list:
    """Return up to max_items recent headlines for symbol_base (e.g. 'INJ', 'BTC').

    Each item: {"title": str, "url": str, "sentiment": "bullish"|"bearish"|"neutral",
                "published_at": str (YYYY-MM-DD)}
    Returns [] on any error or when no headlines match.
    """
    ticker = symbol_base.upper()
    key    = f"{ticker}:{hours}"
    now    = time.monotonic()
    ts, cached = _coin_cache.get(key, (0, []))
    if now - ts < _COIN_CACHE_TTL:
        return cached[:max_items]

    results = _fetch_tiingo(ticker, hours, max_items) or _fetch_rss(ticker, hours, max_items)
    _coin_cache[key] = (now, results)
    return results[:max_items]


def fetch_picks_news(symbols: list, hours: int = 24, max_per_coin: int = 2) -> dict:
    """Fetch news for a list of symbol bases.  Returns {symbol: [news_dicts]}.
    Symbols with no news are omitted. Pre-warms all RSS feeds in one pass."""
    if not TIINGO_KEY:
        for url in _RSS_FEEDS:
            _parse_feed(url)
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
        url   = item.get("url", "")
        lines.append(f"{emoji} [{title}]({url})" if url else f"{emoji} {title}")
    return lines
