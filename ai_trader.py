#!/usr/bin/env python3
"""
AI trading layer: takes TradingView alerts + screener context and produces
structured trade decisions. Includes paper-trading ledger to validate the
system before any real capital.

Two decision functions:
- decide_trade()           : rules-based (default, no API key needed)
- decide_trade_with_llm()  : Claude-powered reasoning (requires anthropic SDK)

Usage:
    python ai_trader.py evaluate    # close paper trades on stop/target hits
    python ai_trader.py report      # P&L summary
"""

import json
import os
import sqlite3
import sys
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

# Must match crypto_agent.py — both services point at the shared volume on
# Railway (e.g. /data/crypto_agent.db) via the same env var.
DB_PATH = Path(os.environ.get("CRYPTO_AGENT_DB", "crypto_agent.db"))

# Load exchange config
def _load_config():
    cfg_path = Path(__file__).parent / "config.json"
    if cfg_path.exists():
        with open(cfg_path) as f:
            return json.load(f)
    return {}

_CFG = _load_config()


# ---- Runtime config overrides (auto-tuned by weight_refitter.py) ----
# The config_overrides table in the DB lets the Sunday refit adjust min_score
# and max_score based on live score-bucket analysis without a git commit or
# redeploy. Values here take precedence over config.json at decision time.

_config_override_cache: tuple = (0.0, {})   # (monotonic_ts, {key: value})


def _get_config_overrides() -> dict:
    """Return DB config overrides. Cached 1 hour so DB isn't hit every webhook."""
    global _config_override_cache
    import time as _time
    now = _time.monotonic()
    ts, cached = _config_override_cache
    if now - ts < 3600:
        return cached
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS config_overrides (
                key TEXT PRIMARY KEY,
                value TEXT,
                updated_at TEXT,
                reason TEXT
            )
        """)
        cur = conn.cursor()
        cur.execute("SELECT key, value FROM config_overrides")
        overrides = {}
        for k, v in cur.fetchall():
            try:
                overrides[k] = json.loads(v)
            except Exception:
                pass
        conn.close()
        _config_override_cache = (now, overrides)
        if overrides:
            print(f"[config] Active overrides: {overrides}", flush=True)
        return overrides
    except Exception:
        return {}


def _effective_score_bounds() -> tuple:
    """(min_score, max_score) with any auto-tune DB overrides applied."""
    ov = _get_config_overrides()
    return (ov.get("min_score", MIN_SCORE_TO_TRADE),
            ov.get("max_score", MAX_SCORE_TO_TRADE))
_SPOT_TAKER_FEE = _CFG.get("exchange", {}).get("spot", {}).get("taker_fee", 0.001)
_PERP_ENABLED   = _CFG.get("exchange", {}).get("perp", {}).get("enabled", True)

# Risk parameters — net R:R accounts for round-trip fees (2 × taker fee)
MIN_SCORE_TO_TRADE  = _CFG.get("risk", {}).get("min_score", 0.5)
MAX_SCORE_TO_TRADE  = _CFG.get("risk", {}).get("max_score", None)  # None = no cap
MAX_POSITION_PCT    = _CFG.get("risk", {}).get("max_position_pct", 5.0)
MIN_RISK_REWARD     = _CFG.get("risk", {}).get("min_risk_reward_gross", 2.0)
MIN_RISK_REWARD_NET = _CFG.get("risk", {}).get("min_risk_reward_net", 1.6)
MIN_HOLD_HOURS      = _CFG.get("risk", {}).get("min_hold_hours", 24)
# Extreme volatility filter: if implied stop distance > X% of entry, the
# market is too volatile for a clean entry (ATR-proxy check from architecture doc).
MAX_STOP_PCT        = _CFG.get("risk", {}).get("max_stop_pct", 8.0)
# Signal max age — reject TradingView alerts older than this (prevent acting on
# stale signals queued during Railway restart or network lag).
MAX_SIGNAL_AGE_SECS = _CFG.get("risk", {}).get("max_signal_age_secs", 120)


@dataclass
class TradeDecision:
    action: str                 # 'enter' | 'pass'
    side: Optional[str]         # 'long' | 'short'
    symbol: str
    entry: Optional[float]
    stop: Optional[float]
    target: Optional[float]
    size_pct: float             # % of portfolio
    confidence: float           # 0-1
    reasoning: str
    decider: str = "rules"                        # 'rules' | 'llm'
    llm_raw_response: Optional[str] = None        # full LLM text, kept for audit
    # ── v2 Signal Engine fields (§7 of agent instruction) ──────────────────
    signal_id: Optional[str] = None              # SYMBOL-TF-UTCtimestamp, for de-dup
    regime: Optional[str] = None                 # RISK_ON | RISK_OFF | CHOP
    regime_reason: Optional[str] = None          # one-line BTC trend summary
    catalyst_strength: str = "none"              # none | weak | strong
    catalyst_note: str = ""                      # headline or "no recent news"
    relative_strength: str = "inline"            # leader | inline | laggard vs BTC
    conviction: str = "low"                      # low | med | high
    thesis: str = ""                             # one plain-English line for Discord
    invalidation: str = ""                       # single condition that means wrong
    target_1_price: Optional[float] = None       # first tranche at 2:1 R:R
    timeframe: str = "1h"

    def to_json(self):
        return json.dumps(asdict(self), indent=2)


# ---------- DB ----------
# New columns added over time. Listed here so init_trading_tables() can
# ALTER existing databases idempotently (SQLite has no ADD COLUMN IF NOT EXISTS).
_ALERT_COLUMNS = [
    ("decider",            "TEXT"),    # 'rules' | 'llm'
    ("decision_action",    "TEXT"),    # 'enter' | 'pass' — even passes get logged
    ("decision_reasoning", "TEXT"),    # parsed reasoning (rules text or LLM-extracted)
    ("llm_raw_response",   "TEXT"),    # full LLM text (only set when decider='llm')
    ("funding_rate_8h",    "REAL"),    # Binance perp 8h funding at alert time; NULL if spot-only or fetch failed
    ("signal_id",          "TEXT"),    # v2: unique signal ID for de-duplication
]
_TRADE_COLUMNS = [
    ("mfe_price",            "REAL"),  # max favorable excursion price
    ("mae_price",            "REAL"),  # max adverse excursion price
    ("time_in_trade_hours",  "REAL"),  # closed_at - opened_at, hours
    ("surprise_ratio",       "REAL"),  # |realized - expected| / max(|expected|,1); < 0.5 = edge, > 1.5 = luck/anomaly
    ("outcome_tag",          "TEXT"),  # 'EDGE' | 'EXPECTED' | 'LUCK' | 'ANOMALY'
    # v2 Signal Engine columns
    ("signal_id",            "TEXT"),  # links to original alert for CLOSE emit
    ("regime",               "TEXT"),  # RISK_ON | RISK_OFF | CHOP at entry
    ("conviction",           "TEXT"),  # low | med | high
    ("thesis",               "TEXT"),  # one-line thesis posted to Discord
    ("invalidation",         "TEXT"),  # condition that means the trade is wrong
    ("target_1_price",       "REAL"),  # first tranche at 2:1 R:R
    ("trailing_stop",        "REAL"),  # current trailing stop (updated after tranche1)
    ("tranche1_closed",      "INTEGER"),  # 1 once first 50% is taken off at target_1
]


def _add_missing_columns(conn, table: str, columns):
    cur = conn.cursor()
    cur.execute(f"PRAGMA table_info({table})")
    existing = {row[1] for row in cur.fetchall()}
    for name, type_ in columns:
        if name not in existing:
            cur.execute(f"ALTER TABLE {table} ADD COLUMN {name} {type_}")


def init_trading_tables():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    # See crypto_agent.init_db for rationale; WAL persists in the file header
    # so re-setting it from this service is idempotent.
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS alerts (
        alert_id INTEGER PRIMARY KEY AUTOINCREMENT,
        received_at TEXT,
        symbol TEXT,
        exchange TEXT,
        side TEXT,
        entry REAL,
        stop REAL,
        target REAL,
        score REAL,
        payload TEXT,
        decider TEXT,
        decision_action TEXT,
        decision_reasoning TEXT,
        llm_raw_response TEXT,
        funding_rate_8h REAL
    );
    CREATE TABLE IF NOT EXISTS paper_trades (
        trade_id INTEGER PRIMARY KEY AUTOINCREMENT,
        alert_id INTEGER,
        opened_at TEXT,
        closed_at TEXT,
        symbol TEXT,
        side TEXT,
        entry_price REAL,
        stop_price REAL,
        target_price REAL,
        size_pct REAL,
        confidence REAL,
        reasoning TEXT,
        status TEXT,           -- 'open' | 'stopped' | 'target'
        exit_price REAL,
        pnl_pct REAL,
        mfe_price REAL,
        mae_price REAL,
        time_in_trade_hours REAL
    );
    """)
    # Idempotent migrations for older DBs that pre-date the new columns.
    _add_missing_columns(conn, "alerts", _ALERT_COLUMNS)
    _add_missing_columns(conn, "paper_trades", _TRADE_COLUMNS)
    # Partial unique index: de-duplicates signals at DB level (NULL values excluded).
    conn.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS idx_paper_trades_signal_id
        ON paper_trades(signal_id) WHERE signal_id IS NOT NULL
    """)
    conn.commit()
    return conn


# ---------- Context lookup ----------
PICKS_MAX_AGE_HOURS = 36  # reject webhook alerts if picks are older than this


def get_screener_context(symbol_base: str, date=None):
    """If this symbol is in the latest screener picks, return rank/score.
    Returns None if picks are missing or stale (> PICKS_MAX_AGE_HOURS old).
    """
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    if not date:
        try:
            cur.execute("SELECT MAX(pick_date) FROM picks")
            date = cur.fetchone()[0]
        except Exception:
            conn.close()
            return None

    if not date:
        conn.close()
        return None

    # Staleness check — use actual elapsed hours, not calendar-day × 24.
    # Screener runs at 13:00 UTC so we anchor age to that time on pick_date.
    try:
        pick_dt = datetime.strptime(date, "%Y-%m-%d")
        pick_run_utc = pick_dt.replace(hour=13, minute=0, second=0, tzinfo=timezone.utc)
        age_hours = (datetime.now(timezone.utc) - pick_run_utc).total_seconds() / 3600.0
        if age_hours > PICKS_MAX_AGE_HOURS:
            conn.close()
            return {"stale": True, "date": date, "age_hours": round(age_hours, 1)}
    except Exception:
        pass

    cur.execute(
        """SELECT rank, composite_score, decorrelation_score,
                  momentum_score, reversal_score
           FROM picks WHERE pick_date = ? AND symbol = ?""",
        (date, symbol_base.upper())
    )
    row = cur.fetchone()
    if row:
        conn.close()
        return {"rank": row[0], "score": row[1], "date": date,
                "decorrelation": round(row[2] or 0.0, 3),
                "momentum_score": round(row[3] or 0.0, 3),
                "reversal_score": round(row[4] or 0.0, 3)}

    # Not in top picks — check full universe (factor_scores) for short-watch candidates
    try:
        cur.execute(
            "SELECT composite_score FROM factor_scores WHERE pick_date = ? AND symbol = ?",
            (date, symbol_base.upper())
        )
        row2 = cur.fetchone()
    except Exception:
        row2 = None
    conn.close()
    if not row2:
        return None
    return {"rank": 999, "score": row2[0], "date": date,
            "decorrelation": 0.0, "is_short_watch": True}


def strip_quote(symbol: str) -> str:
    """BTCUSDT -> BTC, ETHUSD -> ETH."""
    for q in ("USDT", "USDC", "USD", "BUSD"):
        if symbol.upper().endswith(q):
            return symbol[:-len(q)].upper()
    return symbol.upper()


def fetch_funding_rate(symbol: str) -> Optional[float]:
    """Funding rate from Binance Futures. Returns None when perps are disabled
    (Binance.US / US retail) or on any fetch error."""
    if not _PERP_ENABLED:
        return None
    try:
        import requests
        sym = symbol.upper().replace("USD", "USDT").replace("USDTT", "USDT")
        if not sym.endswith("USDT"):
            sym += "USDT"
        r = requests.get(
            "https://fapi.binance.com/fapi/v1/premiumIndex",
            params={"symbol": sym}, timeout=5,
        )
        if r.status_code != 200:
            return None
        rate = r.json().get("lastFundingRate")
        return float(rate) if rate is not None else None
    except Exception:
        return None


def fetch_live_price(symbol: str, exchange: str = "binance") -> Optional[float]:
    """Spot price from Binance.US with Binance.com fallback."""
    try:
        import requests
        sym = symbol.upper()
        if not any(sym.endswith(q) for q in ("USDT", "USDC", "USD", "BUSD")):
            sym += "USDT"
        for base_url in ("https://api.binance.us", "https://api.binance.com"):
            try:
                r = requests.get(
                    f"{base_url}/api/v3/ticker/price",
                    params={"symbol": sym}, timeout=5,
                )
                if r.status_code == 200:
                    price = r.json().get("price")
                    return float(price) if price else None
            except Exception:
                continue
        return None
    except Exception:
        return None


def fetch_binance_ohlcv(symbol: str, since_ts: datetime, interval: str = "4h") -> list:
    """OHLCV candles from Binance.US (Binance.com fallback) since since_ts.
    Returns list of dicts: open_time, open, high, low, close, volume.
    Returns [] on any failure — callers fall back to daily snapshots."""
    try:
        import requests
        sym = symbol.upper()
        if not any(sym.endswith(q) for q in ("USDT", "USDC", "USD", "BUSD")):
            sym += "USDT"
        since_ms = int(since_ts.timestamp() * 1000)
        params = {"symbol": sym, "interval": interval, "startTime": since_ms, "limit": 500}
        for base_url in ("https://api.binance.us", "https://api.binance.com"):
            try:
                r = requests.get(f"{base_url}/api/v3/klines", params=params, timeout=10)
                if r.status_code == 200:
                    return [
                        {
                            "open_time": datetime.fromtimestamp(k[0] / 1000, tz=timezone.utc),
                            "open":  float(k[1]),
                            "high":  float(k[2]),
                            "low":   float(k[3]),
                            "close": float(k[4]),
                            "volume": float(k[5]),
                        }
                        for k in r.json()
                    ]
            except Exception:
                continue
    except Exception:
        pass
    return []


# ---------- Entry condition computation ----------

def compute_entry_conditions(candles: list) -> dict:
    """Compute Pine Script-equivalent entry conditions from Binance candle data.

    Mirrors screener_confirmation.pine exactly so server-side scans and
    TradingView alerts use the same logic. Requires ≥ 50 candles with
    open/high/low/close/volume fields (use fetch_binance_ohlcv).

    Returns a dict with:
      long_ok  — True when all long conditions pass
      short_ok — True when all short conditions pass
      close, atr, rsi, e50, e200, vwma, vol_ratio
    """
    if len(candles) < 50:
        return {"long_ok": False, "short_ok": False}

    closes  = [c["close"]  for c in candles]
    opens   = [c["open"]   for c in candles]
    highs   = [c["high"]   for c in candles]
    lows    = [c["low"]    for c in candles]
    volumes = [c["volume"] for c in candles]
    hlc3    = [(h + l + c) / 3 for h, l, c in zip(highs, lows, closes)]

    # EMA50 and EMA200
    k50, k200 = 2 / 51, 2 / 201
    e50 = e200 = closes[0]
    for p in closes[1:]:
        e50  = p * k50  + e50  * (1 - k50)
        e200 = p * k200 + e200 * (1 - k200)

    # VWMA(hlc3, 20) — mirrors ta.vwma(hlc3, 20) in Pine
    w_sum = sum(hlc3[-20:][i] * volumes[-20:][i] for i in range(20))
    v_sum = sum(volumes[-20:]) or 1
    vwma  = w_sum / v_sum

    # Volume SMA(20)
    vol_avg = sum(volumes[-20:]) / 20
    vol_ratio = volumes[-1] / vol_avg if vol_avg > 0 else 0

    # ATR(14)
    trs = [max(highs[i] - lows[i],
               abs(highs[i] - closes[i - 1]),
               abs(lows[i]  - closes[i - 1]))
           for i in range(1, len(candles))]
    atr = sum(trs[-14:]) / 14

    # RSI(14)
    gains  = [max(closes[i] - closes[i - 1], 0) for i in range(1, len(closes))]
    losses = [max(closes[i - 1] - closes[i], 0) for i in range(1, len(closes))]
    ag, al = sum(gains[-14:]) / 14, sum(losses[-14:]) / 14
    rsi = 100 - 100 / (1 + ag / al) if al > 0 else 100.0

    last_close, last_open = closes[-1], opens[-1]

    # Long conditions (same as Pine Script)
    trend_ok = e50 > e200
    vwap_ok  = last_close > vwma
    vol_ok   = vol_ratio >= 1.5
    green_ok = last_close > last_open
    long_ok  = trend_ok and vwap_ok and vol_ok and green_ok

    # Short conditions. RSI>55, vol 1.2× (relaxed from 60/1.5 — the original
    # combination of RSI>60 + above VWAP + red bar on same 1H candle was <2%
    # frequency even in bear markets, making the scanner nearly unreachable).
    s_trend_ok = e50 <= e200
    s_rsi_ok   = rsi > 55
    s_vwap_ok  = last_close > vwma * 1.005   # within 0.5% above VWAP (was 1%)
    s_vol_ok   = vol_ratio >= 1.2             # distribution volume (was 1.5×)
    red_ok     = last_close < last_open

    # Failed EMA50 bounce: highest high in last 5 bars touched EMA50 zone, now closed below
    highs5      = highs[-5:] if len(highs) >= 5 else highs
    s_bounce_ok  = max(highs5) >= e50 * 0.98 and last_close < e50
    s_ema_reject = s_bounce_ok and vol_ratio >= 1.1 and red_ok

    short_ok = s_trend_ok and ((s_rsi_ok and s_vwap_ok and s_vol_ok and red_ok) or s_ema_reject)

    return {
        "long_ok":   long_ok,
        "short_ok":  short_ok,
        "close":     last_close,
        "atr":       atr,
        "rsi":       round(rsi, 2),
        "e50":       e50,
        "e200":      e200,
        "vwma":      vwma,
        "vol_ratio": round(vol_ratio, 2),
    }


# ---------- BTC regime gate ----------
# Cache stores (monotonic_ts, regime_str, gap_pct, e50, e200, btc_price)
_btc_regime_cache: tuple = (0.0, "bull", 0.0, 0.0, 0.0, 0.0)

_V2_REGIME = {"bull": "RISK_ON", "sideways": "CHOP", "bear": "RISK_OFF"}


def btc_regime() -> str:
    """Returns 'bull', 'sideways', or 'bear' from BTC 4H EMA structure.

    BULL:     EMA50 > EMA200 by > 2%  → full long signals allowed
    SIDEWAYS: EMAs within 2% of each  → decorrelated longs only (decorr > 0.5)
    BEAR:     EMA200 > EMA50 by > 2%  → shorts + strongly decorrelated longs

    Cached 4 hours. Fails to 'bull' on data unavailability so outages never
    silently block all trading.
    """
    global _btc_regime_cache
    import time as _time
    now = _time.monotonic()
    ts = _btc_regime_cache[0]
    if now - ts < 14_400 and ts > 0:
        return _btc_regime_cache[1]
    try:
        since   = datetime.now(timezone.utc) - timedelta(days=36)
        candles = fetch_binance_ohlcv("BTCUSDT", since, "4h")
        closes  = [c["close"] for c in candles]
        if len(closes) < 200:
            _btc_regime_cache = (now, "bull", 0.0, 0.0, 0.0, closes[-1] if closes else 0.0)
            return "bull"
        k50, k200 = 2 / 51, 2 / 201
        e50 = e200 = closes[0]
        for p in closes[1:]:
            e50  = p * k50  + e50  * (1 - k50)
            e200 = p * k200 + e200 * (1 - k200)
        gap_pct = (e50 - e200) / e200 * 100 if e200 else 0
        if   gap_pct >  2.0: result = "bull"
        elif gap_pct < -2.0: result = "bear"
        else:                 result = "sideways"
        _btc_regime_cache = (now, result, gap_pct, e50, e200, closes[-1])
        print(f"[regime] BTC 4H EMA50={e50:.2f} EMA200={e200:.2f} "
              f"gap={gap_pct:+.1f}% → {result.upper()}", flush=True)
        return result
    except Exception:
        # Don't update cache timestamp on failure — next call retries immediately
        # instead of treating a stale "bull" as fresh for another 4 hours.
        return _btc_regime_cache[1] or "bull"


def btc_regime_detail() -> tuple:
    """Returns (v2_regime, reason_str) where v2_regime is RISK_ON/RISK_OFF/CHOP.

    Reads from the same 4H cache as btc_regime() — no extra Binance call.
    """
    btc_regime()  # ensure cache is warm
    _, regime_str, gap_pct, e50, e200, price = _btc_regime_cache
    v2 = _V2_REGIME.get(regime_str, "CHOP")
    price_str = f"price ${price:,.0f}; " if price > 0 else ""
    reason = (
        f"BTC EMA50/200 gap {gap_pct:+.1f}% → {v2}; "
        f"{price_str}"
        f"{'EMA50 above' if e50 > e200 else 'EMA50 below'} EMA200"
    )
    return v2, reason


def btc_regime_bullish() -> bool:
    """Backward-compat alias: True only in full bull regime."""
    return btc_regime() == "bull"


# ---------- v2 Signal Engine helpers ----------

def generate_signal_id(symbol: str, timeframe: str = "1h", ts: datetime = None) -> str:
    """Unique per-bar signal ID for webhook de-duplication (v2 schema §7)."""
    if ts is None:
        ts = datetime.now(timezone.utc)
    return f"{strip_quote(symbol).upper()}-{timeframe.upper()}-{ts.strftime('%Y%m%dT%H%M%SZ')}"


def compute_relative_strength(symbol: str, coin_candles: list) -> str:
    """Compare coin's 24-bar return to BTC's. Returns 'leader', 'inline', or 'laggard'.

    Uses already-fetched coin candles so no extra Binance call for the coin.
    """
    try:
        if len(coin_candles) < 25:
            return "inline"
        btc_candles = fetch_binance_ohlcv("BTCUSDT",
                                          datetime.now(timezone.utc) - timedelta(hours=210), "1h")
        if len(btc_candles) < 25:
            return "inline"
        n = 24
        coin_ret = (coin_candles[-1]["close"] - coin_candles[-n]["close"]) / coin_candles[-n]["close"] * 100
        btc_ret  = (btc_candles[-1]["close"]  - btc_candles[-n]["close"])  / btc_candles[-n]["close"]  * 100
        diff = coin_ret - btc_ret
        if diff > 5:
            return "leader"
        if diff < -5:
            return "laggard"
        return "inline"
    except Exception:
        return "inline"


# Module-level breadth cache — populated by _run_daily in webhook_server.py
_breadth_pct: float = 50.0   # % of sampled coins above their 20-bar 4H MA


def store_breadth(pct: float) -> None:
    """Called by the daily screener after computing breadth."""
    global _breadth_pct
    _breadth_pct = float(pct)


def get_breadth() -> float:
    """Return cached breadth % (default 50 until first daily run)."""
    return _breadth_pct


def _score_catalyst(symbol: str) -> tuple:
    """Best-effort catalyst score using news_fetcher. Returns (strength, note)."""
    try:
        from news_fetcher import fetch_coin_news
        news = fetch_coin_news(strip_quote(symbol), hours=24, max_items=3)
        if not news:
            return "none", "no recent news"
        # High-signal keywords that suggest a genuine catalyst
        strong_kw = {"etf", "listing", "partnership", "mainnet", "upgrade",
                     "acquisition", "launch", "approval", "regulation", "hack",
                     "exploit", "lawsuit", "ban", "halving"}
        titles = " ".join(n.get("title", "").lower() for n in news)
        if any(kw in titles for kw in strong_kw):
            return "strong", news[0].get("title", "")[:80]
        return "weak", news[0].get("title", "")[:80]
    except Exception:
        return "none", "news fetch unavailable"


# ---------- Decision logic ----------

def _enrich(action, side, symbol, entry, stop, target, size_pct, confidence,
            reasoning, v2_regime, v2_reason, ctx, rr,
            rs="inline", cat_str="none", cat_note="", alert_ts=None) -> TradeDecision:
    """Attach all v2 Signal Engine fields to an 'enter' decision."""
    base = strip_quote(symbol)
    risk = abs(entry - stop) if (entry and stop) else 0
    t1 = None
    if risk > 0 and entry is not None:
        t1 = round(entry + 2 * risk, 8) if side == "long" else round(entry - 2 * risk, 8)

    score = ctx.get("score", 1.0) if ctx else 1.0
    regime_aligned = (side == "long" and v2_regime == "RISK_ON") or \
                     (side == "short" and v2_regime == "RISK_OFF")
    factors   = sum([regime_aligned, cat_str == "strong",
                     (side == "long" and rs == "leader") or (side == "short" and rs == "laggard"),
                     score >= 2.0])
    conviction = "high" if factors >= 3 else "med" if factors >= 1 else "low"

    rs_text  = {"leader": "leading BTC", "laggard": "lagging BTC"}.get(rs, "inline with BTC")
    cat_text = f"; catalyst: {cat_note[:60]}" if cat_note and cat_note != "no recent news" else ""
    thesis   = (f"{base} {'long' if side == 'long' else 'short'} — score={score:.2f}, "
                f"{v2_regime}, {rs_text}, R:R {rr:.1f}:1{cat_text}")
    inv = (f"Price closes below {stop:.6g}" if side == "long"
           else f"Price closes above {stop:.6g}")

    return TradeDecision(
        action, side, symbol, entry, stop, target, size_pct, confidence, reasoning,
        signal_id=generate_signal_id(symbol, ts=alert_ts),
        regime=v2_regime, regime_reason=v2_reason,
        catalyst_strength=cat_str, catalyst_note=cat_note,
        relative_strength=rs, conviction=conviction,
        thesis=thesis, invalidation=inv, target_1_price=t1,
    )


def decide_trade(alert: dict) -> TradeDecision:
    """Rules-based decision following the v2 top-down hierarchy:
    Regime → Catalyst → Relative Strength → Structure/Entry.
    """
    symbol = alert.get("symbol", "")
    side   = alert.get("side", "long")
    entry  = alert.get("entry")
    stop   = alert.get("stop")
    target = alert.get("target")

    if not all([entry, stop, target]):
        return TradeDecision("pass", None, symbol, entry, stop, target,
                             0, 0, "Missing entry/stop/target.")

    # Signal latency check — stale signals accumulate during server restarts
    bar_ts = None
    alert_time_str = alert.get("time")
    if alert_time_str and alert.get("source") != "session_scan":
        try:
            alert_ts = datetime.fromisoformat(alert_time_str.replace("Z", "+00:00"))
            bar_ts = alert_ts
            lag = (datetime.now(timezone.utc) - alert_ts).total_seconds()
            if lag > MAX_SIGNAL_AGE_SECS:
                return TradeDecision("pass", side, symbol, entry, stop, target,
                                     0, 0, f"Signal is {lag:.0f}s old (max {MAX_SIGNAL_AGE_SECS}s) "
                                           f"— likely queued during server restart.")
        except Exception:
            pass

    # Risk/reward check (v2: min 2.5:1 gross)
    if side == "long":
        risk, reward = entry - stop, target - entry
    else:
        risk, reward = stop - entry, entry - target
    if risk <= 0 or reward <= 0:
        return TradeDecision("pass", side, symbol, entry, stop, target,
                             0, 0, f"Invalid risk levels (risk={risk}, reward={reward}).")
    rr = reward / risk

    stop_pct = risk / entry * 100 if entry > 0 else 0
    if stop_pct > MAX_STOP_PCT:
        return TradeDecision("pass", side, symbol, entry, stop, target,
                             0, 0.1, f"Stop distance {stop_pct:.1f}% exceeds {MAX_STOP_PCT}% "
                                     f"ceiling — extreme volatility, risk too high.")

    fee_cost = entry * _SPOT_TAKER_FEE * 2
    rr_net = (reward - fee_cost) / (risk + fee_cost) if (risk + fee_cost) > 0 else 0
    if rr < MIN_RISK_REWARD or rr_net < MIN_RISK_REWARD_NET:
        return TradeDecision("pass", side, symbol, entry, stop, target,
                             0, 0.2,
                             f"R:R={rr:.2f} (net={rr_net:.2f}) below "
                             f"{MIN_RISK_REWARD}/{MIN_RISK_REWARD_NET} minimum.")

    # STEP 1 — REGIME (v2 hierarchy)
    regime    = btc_regime()                    # 'bull' | 'sideways' | 'bear'
    v2_regime, v2_reason = btc_regime_detail()  # RISK_ON | CHOP | RISK_OFF + reason

    # STEP 1b — BREADTH FILTER
    # breadth = % of sampled coins above 4H MA20 (computed each daily run, default 50%).
    # Narrow breadth (<30%) while going long = most coins are already below their trend.
    # Wide breadth (>70%) while going short = most coins are rising, shorting is contrarian.
    breadth = get_breadth()
    breadth_note = f" | breadth={breadth:.0f}%"
    breadth_long_penalty  = max(0.6, breadth / 50.0) if breadth < 50 else 1.0
    breadth_short_penalty = max(0.6, (100 - breadth) / 50.0) if breadth > 50 else 1.0

    # STEP 2 — CATALYST (best-effort via news_fetcher)
    base = strip_quote(symbol)
    cat_str, cat_note = _score_catalyst(base)

    # STEP 3 — RELATIVE STRENGTH (from alert's pre-fetched candles if available)
    rs = alert.get("relative_strength", "inline")

    # Cross-reference with screener picks
    ctx = get_screener_context(base)
    if not ctx:
        return TradeDecision("pass", side, symbol, entry, stop, target,
                             0, 0.3, f"{base} not in today's screener top 10.")
    if ctx.get("stale"):
        return TradeDecision("pass", side, symbol, entry, stop, target,
                             0, 0, f"Screener picks are {ctx['age_hours']}h old "
                                   f"(last run: {ctx['date']}). Waiting for fresh data.")

    eff_min, eff_max = _effective_score_bounds()
    is_short_watch = ctx.get("is_short_watch", False)

    if ctx["score"] < eff_min:
        # Short-watch coins (BTC, ETH, BNB, DOGE, SHIB etc.) are valid shorts
        # regardless of score — their negative/low score IS the bearish signal.
        if side == "short" and (ctx["score"] < 0.5 or is_short_watch):
            pass  # allow — weak coin / short-watch is a good short candidate
        else:
            return TradeDecision("pass", side, symbol, entry, stop, target,
                                 0, 0.4,
                                 f"Screener score {ctx['score']:.2f} below {eff_min} threshold.")

    # Skip the 2.0–2.5 score dead zone: 33% hit rate, -0.72% avg 3d return.
    # The 2.5+ range recovers (45.5% hit, +1.87% avg 3d) so we don't cap entirely
    # at 2.0 — we just skip this specific band for longs.
    if side == "long" and 2.0 < ctx["score"] < 2.5:
        return TradeDecision("pass", side, symbol, entry, stop, target,
                             0, 0.3,
                             f"{base} score={ctx['score']:.2f} in 2.0–2.5 dead zone "
                             f"(33% hit rate, -0.72% avg 3d return). Skipping.")

    if eff_max and ctx["score"] > eff_max:
        # Overextended — flip to reversal short in bear/sideways regime.
        # Use implied ATR from the original stop to build correct short levels.
        if side == "long" and regime in ("bear", "sideways"):
            atr_implied = abs(entry - stop) / 1.5   # original stop = 1.5 × ATR
            s_stop   = round(entry + atr_implied * 1.5, 8)   # stop above entry
            s_target = round(entry - atr_implied * 4.0, 8)   # wider target (4×)
            s_rr     = 4.0 / 1.5                              # 2.67:1
            if s_rr >= MIN_RISK_REWARD:
                confidence = min(1.0, (ctx["score"] - eff_max) / 0.5)
                size_pct   = round(MAX_POSITION_PCT * confidence * 0.5, 2)
                reasoning  = (f"{base} score={ctx['score']:.2f} exceeds {eff_max} cap "
                               f"in {regime} — reversal short. R:R={s_rr:.1f}:1.")
                return _enrich("enter", "short", symbol, entry, s_stop, s_target,
                               size_pct, confidence, reasoning,
                               v2_regime, v2_reason, ctx, s_rr, rs, cat_str, cat_note,
                               alert_ts=bar_ts)
        return TradeDecision("pass", side, symbol, entry, stop, target,
                             0, 0.3,
                             f"Screener score {ctx['score']:.2f} above {eff_max} cap.")

    # v2 §8 guardrail: never short in RISK_ON, never long in RISK_OFF (except decorr exception below)
    if side == "short" and regime == "bull":
        return TradeDecision("pass", side, symbol, entry, stop, target,
                             0, 0.2, "RISK_ON — no shorts permitted.")

    # STEP 4 — STRUCTURE / SIZE by regime
    if side == "long":
        decorr = ctx.get("decorrelation", 0.0)
        if regime == "sideways":
            if decorr < 0.5:
                return TradeDecision("pass", side, symbol, entry, stop, target,
                                     0, 0.2,
                                     f"CHOP regime — {base} decorr={decorr:.2f} "
                                     f"too low (need >0.5). Only trade decorrelated coins in chop.")
            confidence = min(1.0, 0.3 + 0.7 * (ctx["score"] - eff_min) / max((eff_max or 3.0) - eff_min, 0.5))
            confidence = round(confidence * breadth_long_penalty, 3)
            size_pct   = round(MAX_POSITION_PCT * confidence * 0.5, 2)
            reasoning  = (f"{base} #{ctx['rank']} score={ctx['score']:.2f} decorr={decorr:.2f}. "
                          f"CHOP — half size. R:R={rr:.2f}.{breadth_note}")
            return _enrich("enter", side, symbol, entry, stop, target,
                           size_pct, confidence, reasoning,
                           v2_regime, v2_reason, ctx, rr, rs, cat_str, cat_note,
                           alert_ts=bar_ts)

        if regime == "bear":
            if decorr < 0.7:
                return TradeDecision("pass", side, symbol, entry, stop, target,
                                     0, 0.2,
                                     f"RISK_OFF — {base} decorr={decorr:.2f} "
                                     f"too low (need >0.7). Coin too correlated to BTC to long.")
            # In RISK_OFF, high-momentum coins are late-stage bounce pumps, not leaders.
            # The refit pushed momentum weight to 53% on bull-era data. Require declining
            # relative momentum (negative momentum_score) before longing in a bear.
            mom_score = ctx.get("momentum_score", 0.0)
            if mom_score > 0.5:
                return TradeDecision("pass", side, symbol, entry, stop, target,
                                     0, 0.2,
                                     f"RISK_OFF — {base} momentum_score={mom_score:.2f} > 0.5. "
                                     f"High momentum in bear regime = late-stage pump, not leader.")
            confidence = min(1.0, 0.3 + 0.7 * (ctx["score"] - eff_min) / max((eff_max or 3.0) - eff_min, 0.5))
            confidence = round(confidence * breadth_long_penalty, 3)
            size_pct   = round(MAX_POSITION_PCT * confidence * 0.25, 2)
            reasoning  = (f"{base} #{ctx['rank']} score={ctx['score']:.2f} decorr={decorr:.2f} "
                          f"mom={mom_score:.2f}. RISK_OFF — quarter size. R:R={rr:.2f}.{breadth_note}")
            return _enrich("enter", side, symbol, entry, stop, target,
                           size_pct, confidence, reasoning,
                           v2_regime, v2_reason, ctx, rr, rs, cat_str, cat_note,
                           alert_ts=bar_ts)

    # Full-size: bull-regime longs + all shorts that cleared the regime gate
    if is_short_watch and side == "short":
        confidence = round(0.5 * breadth_short_penalty, 3)
        size_pct   = round(MAX_POSITION_PCT * confidence * 0.5, 2)
        reasoning  = (f"{base} short-watch score={ctx['score']:.2f} regime={regime}. "
                      f"Weak coin — half size. R:R={rr:.2f}.{breadth_note}")
    else:
        penalty    = breadth_long_penalty if side == "long" else breadth_short_penalty
        confidence = min(1.0, 0.3 + 0.7 * (ctx["score"] - eff_min) / max((eff_max or 3.0) - eff_min, 0.5))
        confidence = round(confidence * penalty, 3)
        size_pct   = round(MAX_POSITION_PCT * confidence, 2)
        reasoning  = (f"{base} #{ctx['rank']} score={ctx['score']:.2f} regime={regime}. "
                      f"R:R={rr:.2f}. Size={size_pct}% conf={confidence:.2f}.{breadth_note}")

    return _enrich("enter", side, symbol, entry, stop, target,
                   size_pct, confidence, reasoning,
                   v2_regime, v2_reason, ctx, rr, rs, cat_str, cat_note,
                   alert_ts=bar_ts)


def decide_trade_with_llm(alert: dict, anthropic_api_key: str) -> TradeDecision:
    """
    Claude-powered decision. Same interface as decide_trade().
    Requires:  pip install anthropic
    """
    import anthropic

    base = strip_quote(alert.get("symbol", ""))
    ctx  = get_screener_context(base)

    # BTC regime gate — skip the LLM call entirely if regime is bearish (saves API cost)
    regime   = btc_regime()
    side_req = alert.get("side", "long")
    if side_req == "long" and regime == "bear":
        decorr = ctx.get("decorrelation", 0.0) if ctx else 0.0
        if decorr < 1.5:
            return TradeDecision("pass", side_req, alert.get("symbol", ""),
                                 alert.get("entry"), alert.get("stop"), alert.get("target"),
                                 0, 0.2,
                                 f"BTC bear regime — decorr={decorr:.2f} insufficient "
                                 f"(need > 1.5 for a long in bear market).",
                                 decider="llm")

    # Reuse the funding rate that log_alert already cached on the dict if
    # present, otherwise fetch — so this works even if called outside the
    # webhook flow (e.g., backtest replays).
    if "funding_rate_8h" not in alert:
        alert["funding_rate_8h"] = fetch_funding_rate(alert.get("symbol", ""))
    funding = alert.get("funding_rate_8h")
    funding_str = f"{funding:.5f} ({funding * 100:.4f}% per 8h)" if funding is not None else "unavailable"

    prompt = f"""You are a disciplined crypto trade reviewer. Evaluate this setup and return ONLY a JSON object - no prose, no code fences.

Alert: {json.dumps(_redact_payload(alert))}
Screener context: {json.dumps(ctx) if ctx else "Not in today's top 10"}
BTC market regime: {regime.upper()} (bull=full longs, sideways=decorrelated longs at half size, bear=shorts + strongly decorrelated longs at quarter size)
Funding rate (8h): {funding_str}

Hard rules:
- Reject if not in screener top 10 (no context).
- Reject if R:R < {MIN_RISK_REWARD}.
- Reject if score < {MIN_SCORE_TO_TRADE}.
- Reject if score > {MAX_SCORE_TO_TRADE} (overextended — late-stage breakout risk).
- In BEAR regime: reject longs unless decorrelation_score > 1.5 in context; use 25% of normal size.
- In SIDEWAYS regime: allow longs with decorrelation_score > 0.5; use 50% of normal size.
- Size 0-{MAX_POSITION_PCT}% based on confidence.

Soft priors (use judgment, don't auto-reject):
- Funding > +0.05% per 8h on a long = crowded longs, mean-reversion risk; consider lower confidence.
- Funding < -0.02% per 8h on a long during uptrend = contrarian tailwind; not necessarily a reason to skip.

Return shape: {{"action":"enter|pass","side":"long|short","entry":N,"stop":N,"target":N,"size_pct":N,"confidence":0-1,"reasoning":"..."}}"""

    client = anthropic.Anthropic(api_key=anthropic_api_key)
    msg = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=500,
        messages=[{"role": "user", "content": prompt}],
    )
    raw_text = msg.content[0].text
    text = raw_text.strip().replace("```json", "").replace("```", "").strip()

    # Always preserve the raw LLM output, even if JSON parsing fails. We need
    # the full text to evaluate later whether the LLM layer is adding signal
    # over the rules-based decider — discarding it on parse error throws away
    # exactly the cases most worth analyzing.
    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        return TradeDecision(
            action="pass", side=alert.get("side"),
            symbol=alert.get("symbol", ""),
            entry=alert.get("entry"), stop=alert.get("stop"), target=alert.get("target"),
            size_pct=0, confidence=0,
            reasoning=f"LLM returned non-JSON ({e}); see llm_raw_response.",
            decider="llm", llm_raw_response=raw_text,
        )

    return TradeDecision(
        action=data["action"], side=data.get("side"),
        symbol=alert.get("symbol", ""),
        entry=data.get("entry"), stop=data.get("stop"), target=data.get("target"),
        size_pct=data.get("size_pct", 0), confidence=data.get("confidence", 0),
        reasoning=data.get("reasoning", ""),
        decider="llm", llm_raw_response=raw_text,
    )


# ---------- Logging / paper trading ----------
_REDACTED_FIELDS = {"auth", "token", "api_key", "secret", "password"}


def _redact_payload(alert: dict) -> dict:
    """Strip secret-looking fields before persisting. The TradingView alert
    payload carries the webhook auth token in plaintext; storing that
    verbatim in alerts.payload means any DB dump or backup leaks the token.
    Always redact at the boundary, never log/persist raw."""
    return {k: ("***REDACTED***" if k.lower() in _REDACTED_FIELDS else v)
            for k, v in alert.items()}


def log_alert(alert: dict) -> int:
    # Enrich with funding rate at alert time. Mutates `alert` so any
    # downstream caller (decide_trade_with_llm) can read it without a second
    # network round-trip. Idempotent: if caller already populated it, skip.
    if "funding_rate_8h" not in alert:
        alert["funding_rate_8h"] = fetch_funding_rate(alert.get("symbol", ""))

    conn = init_trading_tables()
    cur  = conn.cursor()
    cur.execute("""
        INSERT INTO alerts
        (received_at, symbol, exchange, side, entry, stop, target, score, payload, funding_rate_8h)
        VALUES (?,?,?,?,?,?,?,?,?,?)
    """, (
        datetime.now(timezone.utc).isoformat(),
        alert.get("symbol"), alert.get("exchange"), alert.get("side"),
        alert.get("entry"), alert.get("stop"), alert.get("target"),
        alert.get("score"),
        json.dumps(_redact_payload(alert)),     # strip auth/token before persisting
        alert.get("funding_rate_8h"),
    ))
    aid = cur.lastrowid
    conn.commit()
    conn.close()
    return aid


def log_decision(alert_id: int, d: TradeDecision):
    """Persist decider output back onto the alert row (entry and pass both logged)."""
    conn = init_trading_tables()
    cur  = conn.cursor()
    cur.execute("""
        UPDATE alerts
        SET decider = ?, decision_action = ?, decision_reasoning = ?,
            llm_raw_response = ?, signal_id = ?
        WHERE alert_id = ?
    """, (d.decider, d.action, d.reasoning, d.llm_raw_response, d.signal_id, alert_id))
    conn.commit()
    conn.close()


def open_paper_trade(d: TradeDecision, alert_id: int):
    if d.action != "enter":
        return None
    conn = init_trading_tables()
    cur  = conn.cursor()
    cur.execute("""
        INSERT INTO paper_trades
        (alert_id, opened_at, symbol, side, entry_price, stop_price, target_price,
         size_pct, confidence, reasoning, status,
         signal_id, regime, conviction, thesis, invalidation,
         target_1_price, trailing_stop, tranche1_closed)
        VALUES (?,?,?,?,?,?,?,?,?,?,'open', ?,?,?,?,?,?,?,0)
    """, (
        alert_id, datetime.now(timezone.utc).isoformat(),
        d.symbol, d.side, d.entry, d.stop, d.target,
        d.size_pct, d.confidence, d.reasoning,
        d.signal_id, d.regime, d.conviction, d.thesis, d.invalidation,
        d.target_1_price, d.stop,   # trailing_stop starts at original stop
    ))
    tid = cur.lastrowid
    conn.commit()
    conn.close()

    # News fetch is best-effort — never allowed to block the Discord notification.
    news = []
    try:
        from news_fetcher import fetch_coin_news
        news = fetch_coin_news(strip_quote(d.symbol), hours=24, max_items=3)
    except Exception as e:
        print(f"[news] Fetch failed for {d.symbol}: {e}", flush=True)

    try:
        from notifier import notify_trade_opened
        notify_trade_opened({
            "trade_id": tid, "symbol": d.symbol, "side": d.side,
            "entry_price": d.entry, "stop_price": d.stop, "target_price": d.target,
            "size_pct": d.size_pct, "confidence": d.confidence, "reasoning": d.reasoning,
            # v2 fields
            "signal_id": d.signal_id, "regime": d.regime, "regime_reason": d.regime_reason,
            "conviction": d.conviction, "thesis": d.thesis, "invalidation": d.invalidation,
            "catalyst_strength": d.catalyst_strength, "catalyst_note": d.catalyst_note,
            "relative_strength": d.relative_strength,
            "target_1_price": d.target_1_price,
        }, news=news)
    except Exception as e:
        print(f"[notifier] Trade open notify failed for #{tid}: {e}", flush=True)

    return tid


def _surprise_ratio(realized_pnl: float, entry: float, stop: float,
                    target: float, confidence: float, side: str) -> tuple:
    """Compute Surprise Ratio from architecture blueprint.

    Expected PnL = confidence × target_pct + (1-confidence) × stop_pct
    Surprise     = |realized - expected| / max(|expected|, 1)

    Tags: EDGE (< 0.5) — setup worked as designed
          EXPECTED (0.5–1.5) — within normal variance
          LUCK (> 1.5, winner) — lucky anomaly, discount in future
          ANOMALY (> 1.5, loser) — unlucky shock, discount in future
    """
    if not entry or not stop or not target:
        return None, "UNKNOWN"
    conf = max(0.0, min(1.0, confidence or 0.0))
    if side == "long":
        tgt_pct  = (target - entry) / entry * 100
        stop_pct = (stop   - entry) / entry * 100
    else:
        tgt_pct  = (entry - target) / entry * 100
        stop_pct = (entry - stop)   / entry * 100
    expected = conf * tgt_pct + (1 - conf) * stop_pct
    surprise = abs(realized_pnl - expected) / max(abs(expected), 1.0)
    if surprise < 0.5:
        tag = "EDGE"
    elif surprise < 1.5:
        tag = "EXPECTED"
    else:
        tag = "LUCK" if realized_pnl > 0 else "ANOMALY"
    return round(surprise, 3), tag


def evaluate_open_trades_live():
    """Check open paper trades against current live prices.

    v2 behaviour:
    - Tranche 1 (50 %) closes at target_1_price (2:1 R:R). Stop moves to breakeven.
    - Remainder trails by implied 1.5×ATR (derived from original stop distance).
    - Full target_price closes remaining 50 %.
    - CLOSE signal is posted to Discord on every closure so followers know to exit.
    """
    conn = init_trading_tables()
    cur  = conn.cursor()
    cur.execute("SELECT * FROM paper_trades WHERE status = 'open'")
    cols = [d[0] for d in cur.description]
    rows = [dict(zip(cols, r)) for r in cur.fetchall()]

    closed = 0
    pending_notifs = []
    now    = datetime.now(timezone.utc)
    for t in rows:
        try:
            opened_at = datetime.fromisoformat(t["opened_at"])
            if opened_at.tzinfo is None:
                opened_at = opened_at.replace(tzinfo=timezone.utc)
        except (TypeError, ValueError):
            continue

        # Guard full exits only — tranche1 partial close and trailing stop updates
        # run regardless of hold time so fast movers get their T1 card and breakeven stop.
        too_young = (now - opened_at).total_seconds() < max(MIN_HOLD_HOURS * 3600, 1800)

        last_price = fetch_live_price(t["symbol"])
        if last_price is None:
            continue

        side      = t["side"]
        entry     = t["entry_price"]
        orig_stop = t["stop_price"]
        tgt_p     = t["target_price"]
        t1_p      = t.get("target_1_price")          # first tranche (2:1)
        trail_p   = t.get("trailing_stop") or orig_stop
        t1_closed = bool(t.get("tranche1_closed"))

        # Implied ATR from original stop distance (stored as 1.5×ATR)
        atr = abs(entry - orig_stop) / 1.5 if orig_stop else 0

        # ── Step A: update trailing stop after tranche1 ──────────────────────
        if t1_closed and atr > 0:
            if side == "long":
                new_trail = last_price - atr * 1.5
                # ratchet up only; never below breakeven
                new_trail = max(new_trail, entry)
                if new_trail > trail_p:
                    trail_p = new_trail
                    cur.execute("UPDATE paper_trades SET trailing_stop=? WHERE trade_id=?",
                                (trail_p, t["trade_id"]))
            else:
                new_trail = last_price + atr * 1.5
                new_trail = min(new_trail, entry)  # ratchet down; never above breakeven
                if new_trail < trail_p:
                    trail_p = new_trail
                    cur.execute("UPDATE paper_trades SET trailing_stop=? WHERE trade_id=?",
                                (trail_p, t["trade_id"]))

        # ── Step B: check tranche1 hit ────────────────────────────────────────
        if not t1_closed and t1_p:
            t1_hit = (side == "long" and last_price >= t1_p) or \
                     (side == "short" and last_price <= t1_p)
            if t1_hit:
                t1_pnl = (t1_p / entry - 1) * 100 if side == "long" else (entry - t1_p) / entry * 100
                # Move trailing stop to breakeven
                cur.execute("""
                    UPDATE paper_trades
                    SET tranche1_closed=1, trailing_stop=?
                    WHERE trade_id=?
                """, (entry, t["trade_id"]))
                trail_p   = entry
                t1_closed = True
                print(f"[live-eval] #{t['trade_id']} {t['symbol']} T1 hit @ {t1_p:.6g} "
                      f"(+{t1_pnl:.2f}%) — stop moved to breakeven.", flush=True)
                pending_notifs.append({
                    "trade_id": t["trade_id"], "symbol": t["symbol"],
                    "status": "tranche1", "pnl_pct": t1_pnl,
                    "entry_price": entry, "exit_price": t1_p,
                    "size_pct": (t.get("size_pct") or 0) * 0.5,
                    "signal_id": t.get("signal_id"),
                    "thesis": t.get("thesis", ""),
                })

        # ── Step C: check full close (trailing stop or full target) ──────────
        status, exit_p = None, None
        if side == "long":
            if last_price <= trail_p:
                status, exit_p = "stopped", trail_p
            elif last_price >= tgt_p:
                status, exit_p = "target", tgt_p
        else:
            if last_price >= trail_p:
                status, exit_p = "stopped", trail_p
            elif last_price <= tgt_p:
                status, exit_p = "target", tgt_p

        if status and not too_young:
            time_in_h = (now - opened_at).total_seconds() / 3600.0
            pnl = (exit_p / entry - 1) * 100 if side == "long" else (entry - exit_p) / entry * 100
            surprise, tag = _surprise_ratio(
                pnl, entry, orig_stop, tgt_p, t.get("confidence", 0.5), side
            )
            # Best-effort MFE/MAE for live-priced closes (we have exit price, not path).
            # Fills NULL only — snapshot evaluator values are kept if already set.
            mfe_est = tgt_p if status == "target" else (
                t.get("target_1_price") or exit_p if t1_closed else exit_p)
            mae_est = exit_p if status == "stopped" else entry
            cur.execute("""
                UPDATE paper_trades
                SET status=?, closed_at=?, exit_price=?, pnl_pct=?,
                    time_in_trade_hours=?, surprise_ratio=?, outcome_tag=?,
                    mfe_price=COALESCE(mfe_price, ?), mae_price=COALESCE(mae_price, ?)
                WHERE trade_id=?
            """, (status, now.isoformat(), exit_p, pnl,
                  time_in_h, surprise, tag, mfe_est, mae_est, t["trade_id"]))
            closed += 1
            label = "trailing stop" if (t1_closed and status == "stopped") else status
            print(f"[live-eval] #{t['trade_id']} {t['symbol']} {label.upper()} "
                  f"P&L {pnl:+.2f}% | {tag}", flush=True)
            pending_notifs.append({
                "trade_id": t["trade_id"], "symbol": t["symbol"],
                "status": status, "pnl_pct": pnl,
                "entry_price": entry, "exit_price": exit_p,
                "size_pct": t.get("size_pct"),
                "signal_id": t.get("signal_id"),
                "thesis": t.get("thesis", ""),
                "outcome_tag": tag,
                "tranche1_already_closed": t1_closed,
            })

    conn.commit()
    conn.close()
    for payload in pending_notifs:
        try:
            from notifier import notify_trade_closed
            notify_trade_closed(payload)
        except Exception as e:
            print(f"[notifier] Notify failed #{payload.get('trade_id')}: {e}", flush=True)
    if closed:
        print(f"[live-eval] Closed {closed} trade(s) on live prices.", flush=True)
    return closed


def evaluate_open_trades():
    """Check open paper trades against Binance 4H candles since open.

    Uses 4H candle high/low for accurate intraday stop/target detection and
    MFE/MAE tracking. Falls back to daily CoinGecko snapshots when Binance
    data is unavailable (e.g. coin not listed there).
    """
    conn = init_trading_tables()
    cur  = conn.cursor()
    cur.execute("SELECT * FROM paper_trades WHERE status = 'open'")
    cols = [d[0] for d in cur.description]
    rows = [dict(zip(cols, r)) for r in cur.fetchall()]

    closed = 0
    now = datetime.now(timezone.utc)
    for t in rows:
        base   = strip_quote(t["symbol"])
        side   = t["side"]
        entry  = t["entry_price"]
        # Use trailing_stop if set (moved to breakeven after T1); fall back to original stop.
        stop_p = t.get("trailing_stop") or t["stop_price"]
        tgt_p  = t["target_price"]

        opened_at_iso = t["opened_at"]
        try:
            opened_at = datetime.fromisoformat(opened_at_iso)
            if opened_at.tzinfo is None:
                opened_at = opened_at.replace(tzinfo=timezone.utc)
        except (TypeError, ValueError):
            continue

        # Skip stop-check if under minimum hold time — let MFE/MAE update only.
        under_min_hold = (now - opened_at).total_seconds() < MIN_HOLD_HOURS * 3600

        mfe = t.get("mfe_price") or entry
        mae = t.get("mae_price") or entry
        status, exit_p, exit_ts = None, None, None

        # ── Try Binance 4H candles (higher resolution, uses hi/lo per bar) ──
        candles = fetch_binance_ohlcv(t["symbol"], opened_at, "4h")
        if candles:
            for c in candles:
                hi, lo = c["high"], c["low"]
                if side == "long":
                    if hi > mfe: mfe = hi
                    if lo < mae: mae = lo
                    if not under_min_hold:
                        # Conservative: if stop and target both breached, take stop
                        if lo <= stop_p:
                            status, exit_p, exit_ts = "stopped", stop_p, c["open_time"]
                            break
                        if hi >= tgt_p:
                            status, exit_p, exit_ts = "target", tgt_p, c["open_time"]
                            break
                else:
                    if lo < mfe: mfe = lo
                    if hi > mae: mae = hi
                    if not under_min_hold:
                        if hi >= stop_p:
                            status, exit_p, exit_ts = "stopped", stop_p, c["open_time"]
                            break
                        if lo <= tgt_p:
                            status, exit_p, exit_ts = "target", tgt_p, c["open_time"]
                            break
        else:
            # ── Fallback: daily CoinGecko snapshots ──────────────────────────
            opened_date = opened_at.date().isoformat()
            cur.execute("""
                SELECT snapshot_date, price FROM snapshots
                WHERE symbol = ? AND snapshot_date >= ?
                ORDER BY snapshot_date ASC
            """, (base, opened_date))
            path = cur.fetchall()
            if not path:
                continue
            for snap_date, price in path:
                if not price:
                    continue
                if side == "long":
                    if price > mfe: mfe = price
                    if price < mae: mae = price
                    if not under_min_hold:
                        if price <= stop_p:
                            status, exit_p, exit_ts = "stopped", stop_p, snap_date
                            break
                        if price >= tgt_p:
                            status, exit_p, exit_ts = "target", tgt_p, snap_date
                            break
                else:
                    if price < mfe: mfe = price
                    if price > mae: mae = price
                    if not under_min_hold:
                        if price >= stop_p:
                            status, exit_p, exit_ts = "stopped", stop_p, snap_date
                            break
                        if price <= tgt_p:
                            status, exit_p, exit_ts = "target", tgt_p, snap_date
                            break

        if status:
            if isinstance(exit_ts, datetime):
                closed_at = exit_ts
            else:
                try:
                    closed_at = datetime.fromisoformat(exit_ts).replace(
                        hour=23, minute=59, second=59, tzinfo=timezone.utc
                    )
                except (TypeError, ValueError):
                    closed_at = now
            time_in_trade_h = (closed_at - opened_at).total_seconds() / 3600.0
            pnl = (exit_p / entry - 1) * 100 if side == "long" else (entry - exit_p) / entry * 100
            surprise, tag = _surprise_ratio(
                pnl, entry, t["stop_price"], t["target_price"], t.get("confidence", 0.5), t["side"]
            )
            cur.execute("""
                UPDATE paper_trades
                SET status=?, closed_at=?, exit_price=?, pnl_pct=?,
                    mfe_price=?, mae_price=?, time_in_trade_hours=?,
                    surprise_ratio=?, outcome_tag=?
                WHERE trade_id=?
            """, (status, closed_at.isoformat(), exit_p, pnl,
                  mfe, mae, time_in_trade_h, surprise, tag, t["trade_id"]))
            closed += 1
            try:
                from notifier import notify_trade_closed
                notify_trade_closed({
                    "trade_id": t["trade_id"], "symbol": t["symbol"],
                    "status": status, "pnl_pct": pnl,
                    "entry_price": entry, "exit_price": exit_p,
                    "size_pct": t["size_pct"],
                })
            except Exception as e:
                print(f"[notifier] Trade close notify failed for #{t['trade_id']}: {e}", flush=True)
        else:
            cur.execute("""
                UPDATE paper_trades SET mfe_price=?, mae_price=?
                WHERE trade_id=?
            """, (mfe, mae, t["trade_id"]))

    conn.commit()
    conn.close()
    print(f"Closed {closed} paper trades.")
    return closed


def report_paper_trades():
    conn = init_trading_tables()
    cur  = conn.cursor()
    print("=== Paper trading P&L ===")
    cur.execute("""
        SELECT status, COUNT(*), AVG(pnl_pct), SUM(pnl_pct * size_pct / 100)
        FROM paper_trades WHERE status != 'open' GROUP BY status
    """)
    rows = cur.fetchall()
    if rows:
        print(f"{'Status':<12}{'Trades':<10}{'Avg %':<14}{'Weighted %':<14}")
        for status, n, avg, weighted in rows:
            print(f"{status:<12}{n:<10}{avg or 0:+.2f}%        {weighted or 0:+.2f}%")
    else:
        print("No closed trades yet.")
    cur.execute("SELECT COUNT(*) FROM paper_trades WHERE status='open'")
    print(f"Open positions: {cur.fetchone()[0]}")

    # MFE/MAE and time-in-trade — these are the diagnostics that tell you
    # *why* the win rate is what it is. High avg MAE on winners = stops too
    # tight; low avg MFE on losers = thesis was wrong, not just noise.
    print("\n=== Excursion + duration (closed trades) ===")
    cur.execute("""
        SELECT status,
               AVG((mfe_price/entry_price - 1) * 100 *
                   CASE WHEN side='long' THEN 1 ELSE -1 END) AS avg_mfe_pct,
               AVG((mae_price/entry_price - 1) * 100 *
                   CASE WHEN side='long' THEN 1 ELSE -1 END) AS avg_mae_pct,
               AVG(time_in_trade_hours) AS avg_hours,
               COUNT(*) AS n
        FROM paper_trades
        WHERE status != 'open' AND mfe_price IS NOT NULL
        GROUP BY status
    """)
    rows = cur.fetchall()
    if rows:
        print(f"{'Status':<12}{'n':<6}{'Avg MFE%':<12}{'Avg MAE%':<12}{'Avg hrs':<10}")
        for status, mfe_pct, mae_pct, hrs, n in rows:
            print(f"{status:<12}{n:<6}"
                  f"{mfe_pct or 0:+.2f}%      "
                  f"{mae_pct or 0:+.2f}%      "
                  f"{hrs or 0:.1f}")
    else:
        print("No closed trades with excursion data yet.")

    # LLM-vs-rules disagreement summary — only meaningful once both have run.
    cur.execute("""
        SELECT decider, decision_action, COUNT(*) FROM alerts
        WHERE decider IS NOT NULL
        GROUP BY decider, decision_action
        ORDER BY decider, decision_action
    """)
    rows = cur.fetchall()
    if rows:
        print("\n=== Decider activity ===")
        for decider, action, n in rows:
            print(f"  {decider:<6} {action:<6} n={n}")

    conn.close()


if __name__ == "__main__":
    if len(sys.argv) < 2 or sys.argv[1] not in ("evaluate", "report"):
        print("Usage: python ai_trader.py [evaluate|report]")
        sys.exit(1)
    if sys.argv[1] == "evaluate":
        evaluate_open_trades()
    else:
        report_paper_trades()
