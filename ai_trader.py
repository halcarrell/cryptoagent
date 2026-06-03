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
_SPOT_TAKER_FEE = _CFG.get("exchange", {}).get("spot", {}).get("taker_fee", 0.001)
_PERP_ENABLED   = _CFG.get("exchange", {}).get("perp", {}).get("enabled", True)

# Risk parameters — net R:R accounts for round-trip fees (2 × taker fee)
MIN_SCORE_TO_TRADE  = _CFG.get("risk", {}).get("min_score", 0.5)
MAX_SCORE_TO_TRADE  = _CFG.get("risk", {}).get("max_score", None)  # None = no cap
MAX_POSITION_PCT    = _CFG.get("risk", {}).get("max_position_pct", 5.0)
MIN_RISK_REWARD     = _CFG.get("risk", {}).get("min_risk_reward_gross", 2.0)
MIN_RISK_REWARD_NET = _CFG.get("risk", {}).get("min_risk_reward_net", 1.6)
# Minimum hours a trade must be open before a stop can trigger. Cross-sectional
# momentum has a 7-day optimal holding period; 24h prevents 4H noise from
# closing a position before its thesis has time to develop.
MIN_HOLD_HOURS      = _CFG.get("risk", {}).get("min_hold_hours", 24)


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
]
_TRADE_COLUMNS = [
    ("mfe_price",            "REAL"),  # max favorable excursion price
    ("mae_price",            "REAL"),  # max adverse excursion price
    ("time_in_trade_hours",  "REAL"),  # closed_at - opened_at, hours
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

    # Staleness check
    from datetime import date as _date
    today = datetime.now(timezone.utc).date()
    try:
        pick_dt = datetime.strptime(date, "%Y-%m-%d").date()
        age_hours = (today - pick_dt).days * 24
        if age_hours > PICKS_MAX_AGE_HOURS:
            conn.close()
            return {"stale": True, "date": date, "age_hours": age_hours}
    except Exception:
        pass

    cur.execute(
        "SELECT rank, composite_score FROM picks WHERE pick_date = ? AND symbol = ?",
        (date, symbol_base.upper())
    )
    row = cur.fetchone()
    conn.close()
    return {"rank": row[0], "score": row[1], "date": date} if row else None


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
    """4H OHLCV candles from Binance.US (Binance.com fallback) since since_ts.
    Returns list of dicts with open_time (datetime), high, low, close.
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
                            "high":  float(k[2]),
                            "low":   float(k[3]),
                            "close": float(k[4]),
                        }
                        for k in r.json()
                    ]
            except Exception:
                continue
    except Exception:
        pass
    return []


# ---------- BTC regime gate ----------
_btc_regime_cache: tuple = (0.0, True)  # (monotonic_ts, result) — 4H TTL


def btc_regime_bullish() -> bool:
    """True when BTC 4H EMA50 > EMA200 (uptrend intact).

    Cached for 4 hours — same cadence as 4H bar closes, so the value only
    updates when new market structure information is available.
    Fails open (returns True) so a Binance outage never silently blocks trades.
    """
    global _btc_regime_cache
    import time as _time
    now = _time.monotonic()
    ts, cached = _btc_regime_cache
    if now - ts < 14_400:          # 4-hour cache
        return cached
    try:
        since   = datetime.now(timezone.utc) - timedelta(days=36)  # ~216 bars
        candles = fetch_binance_ohlcv("BTCUSDT", since, "4h")
        closes  = [c["close"] for c in candles]
        if len(closes) < 200:
            return True            # not enough history yet — don't block
        # Incremental EMA so we don't need numpy
        k50, k200 = 2 / 51, 2 / 201
        e50 = e200 = closes[0]
        for p in closes[1:]:
            e50  = p * k50  + e50  * (1 - k50)
            e200 = p * k200 + e200 * (1 - k200)
        result = e50 > e200
        _btc_regime_cache = (now, result)
        print(f"[regime] BTC 4H EMA50={e50:.2f} EMA200={e200:.2f} "
              f"→ {'BULL ✓' if result else 'BEAR — blocking altcoin longs'}", flush=True)
        return result
    except Exception:
        return True                # fail open


# ---------- Decision logic ----------
def decide_trade(alert: dict) -> TradeDecision:
    """Rules-based decision. Replace with LLM for fancier reasoning."""
    symbol = alert.get("symbol", "")
    side   = alert.get("side", "long")
    entry  = alert.get("entry")
    stop   = alert.get("stop")
    target = alert.get("target")

    if not all([entry, stop, target]):
        return TradeDecision("pass", None, symbol, entry, stop, target,
                             0, 0, "Missing entry/stop/target.")

    # Risk/reward check
    if side == "long":
        risk, reward = entry - stop, target - entry
    else:
        risk, reward = stop - entry, entry - target
    if risk <= 0 or reward <= 0:
        return TradeDecision("pass", side, symbol, entry, stop, target,
                             0, 0, f"Invalid risk levels (risk={risk}, reward={reward}).")
    rr = reward / risk
    # Fee-adjusted: subtract round-trip taker fees (entry + exit) from reward and add to risk
    fee_cost = entry * _SPOT_TAKER_FEE * 2
    rr_net = (reward - fee_cost) / (risk + fee_cost) if (risk + fee_cost) > 0 else 0
    if rr < MIN_RISK_REWARD or rr_net < MIN_RISK_REWARD_NET:
        return TradeDecision("pass", side, symbol, entry, stop, target,
                             0, 0.2,
                             f"R:R={rr:.2f} (net={rr_net:.2f}) below "
                             f"{MIN_RISK_REWARD}/{MIN_RISK_REWARD_NET} minimum.")

    # Cross-reference with screener picks
    base = strip_quote(symbol)
    ctx  = get_screener_context(base)
    if not ctx:
        return TradeDecision("pass", side, symbol, entry, stop, target,
                             0, 0.3, f"{base} not in today's screener top 10.")
    if ctx.get("stale"):
        return TradeDecision("pass", side, symbol, entry, stop, target,
                             0, 0, f"Screener picks are {ctx['age_hours']}h old "
                                   f"(last run: {ctx['date']}). Waiting for fresh data.")
    if ctx["score"] < MIN_SCORE_TO_TRADE:
        return TradeDecision("pass", side, symbol, entry, stop, target,
                             0, 0.4,
                             f"Screener score {ctx['score']:.2f} below {MIN_SCORE_TO_TRADE}.")

    if MAX_SCORE_TO_TRADE and ctx["score"] > MAX_SCORE_TO_TRADE:
        return TradeDecision("pass", side, symbol, entry, stop, target,
                             0, 0.3,
                             f"Screener score {ctx['score']:.2f} above {MAX_SCORE_TO_TRADE} cap "
                             f"— likely overextended/late-stage breakout.")

    # BTC regime gate — only after screener passes so we don't burn a Binance
    # request on every rejected signal. Fails open; short setups are exempt.
    if side == "long" and not btc_regime_bullish():
        return TradeDecision("pass", side, symbol, entry, stop, target,
                             0, 0.2,
                             "BTC 4H EMA50 < EMA200 — bearish market regime, "
                             "skipping altcoin long to avoid headwind.")

    # Position sizing scales with confidence
    confidence = min(1.0, max(0.0, ctx["score"]))
    size_pct   = round(MAX_POSITION_PCT * confidence, 2)

    reasoning = (
        f"{base} ranked #{ctx['rank']} in screener (score={ctx['score']:.2f}). "
        f"Pine confirmation fired with R:R={rr:.2f}. "
        f"Sizing {size_pct}% on confidence {confidence:.2f}."
    )
    return TradeDecision("enter", side, symbol, entry, stop, target,
                         size_pct, confidence, reasoning)


def decide_trade_with_llm(alert: dict, anthropic_api_key: str) -> TradeDecision:
    """
    Claude-powered decision. Same interface as decide_trade().
    Requires:  pip install anthropic
    """
    import anthropic

    base = strip_quote(alert.get("symbol", ""))
    ctx  = get_screener_context(base)

    # BTC regime gate — skip the LLM call entirely if regime is bearish (saves API cost)
    if alert.get("side", "long") == "long" and not btc_regime_bullish():
        return TradeDecision("pass", alert.get("side"), alert.get("symbol", ""),
                             alert.get("entry"), alert.get("stop"), alert.get("target"),
                             0, 0.2,
                             "BTC 4H EMA50 < EMA200 — bearish market regime, "
                             "skipping altcoin long to avoid headwind.",
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
Funding rate (8h): {funding_str}

Hard rules:
- Reject if not in screener top 10 (no context).
- Reject if R:R < {MIN_RISK_REWARD}.
- Reject if score < {MIN_SCORE_TO_TRADE}.
- Reject if score > {MAX_SCORE_TO_TRADE} (overextended — late-stage breakout risk).
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
    """Persist decider output back onto the alert row. Called for every
    decision — entry OR pass. Without this, 'pass' decisions vanish and we
    can never measure how often the LLM disagrees with the rules, or what
    it was reasoning about when it did."""
    conn = init_trading_tables()
    cur  = conn.cursor()
    cur.execute("""
        UPDATE alerts
        SET decider = ?, decision_action = ?, decision_reasoning = ?, llm_raw_response = ?
        WHERE alert_id = ?
    """, (d.decider, d.action, d.reasoning, d.llm_raw_response, alert_id))
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
         size_pct, confidence, reasoning, status)
        VALUES (?,?,?,?,?,?,?,?,?,?, 'open')
    """, (
        alert_id, datetime.now(timezone.utc).isoformat(),
        d.symbol, d.side, d.entry, d.stop, d.target,
        d.size_pct, d.confidence, d.reasoning,
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
        }, news=news)
    except Exception as e:
        print(f"[notifier] Trade open notify failed for #{tid}: {e}", flush=True)

    return tid


def evaluate_open_trades_live():
    """Check open paper trades against current live prices (Binance.US → Binance.com).

    Runs every 4 hours via the scheduler so stop/target hits are caught intraday
    rather than waiting for the next morning's daily snapshot run.
    Skips trades opened less than 30 minutes ago to avoid closing on entry noise.
    """
    conn = init_trading_tables()
    cur  = conn.cursor()
    cur.execute("SELECT * FROM paper_trades WHERE status = 'open'")
    cols = [d[0] for d in cur.description]
    rows = [dict(zip(cols, r)) for r in cur.fetchall()]

    closed = 0
    now    = datetime.now(timezone.utc)
    for t in rows:
        # Don't evaluate trades younger than MIN_HOLD_HOURS. Cross-sectional
        # momentum needs time to develop; closing on the first 4H wiggle wastes
        # the entry signal. Hard floor of 30 min to handle clock skew.
        try:
            opened_at = datetime.fromisoformat(t["opened_at"])
            if opened_at.tzinfo is None:
                opened_at = opened_at.replace(tzinfo=timezone.utc)
            min_secs = max(MIN_HOLD_HOURS * 3600, 1800)
            if (now - opened_at).total_seconds() < min_secs:
                continue
        except (TypeError, ValueError):
            continue

        last_price = fetch_live_price(t["symbol"])
        if last_price is None:
            continue

        side   = t["side"]
        entry  = t["entry_price"]
        stop_p = t["stop_price"]
        tgt_p  = t["target_price"]

        status, exit_p = None, None
        if side == "long":
            if last_price <= stop_p:
                status, exit_p = "stopped", stop_p
            elif last_price >= tgt_p:
                status, exit_p = "target", tgt_p
        else:
            if last_price >= stop_p:
                status, exit_p = "stopped", stop_p
            elif last_price <= tgt_p:
                status, exit_p = "target", tgt_p

        if status:
            time_in_trade_h = (now - opened_at).total_seconds() / 3600.0
            pnl = (exit_p / entry - 1) * 100 if side == "long" else (entry / exit_p - 1) * 100
            cur.execute("""
                UPDATE paper_trades
                SET status=?, closed_at=?, exit_price=?, pnl_pct=?, time_in_trade_hours=?
                WHERE trade_id=?
            """, (status, now.isoformat(), exit_p, pnl, time_in_trade_h, t["trade_id"]))
            closed += 1
            print(f"[live-eval] Trade #{t['trade_id']} {t['symbol']} {status.upper()} "
                  f"@ {last_price} (entry {entry}), P&L {pnl:+.2f}%", flush=True)
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

    conn.commit()
    conn.close()
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
        stop_p = t["stop_price"]
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
            pnl = (exit_p / entry - 1) * 100 if side == "long" else (entry / exit_p - 1) * 100
            cur.execute("""
                UPDATE paper_trades
                SET status=?, closed_at=?, exit_price=?, pnl_pct=?,
                    mfe_price=?, mae_price=?, time_in_trade_hours=?
                WHERE trade_id=?
            """, (status, closed_at.isoformat(), exit_p, pnl,
                  mfe, mae, time_in_trade_h, t["trade_id"]))
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
