#!/usr/bin/env python3
"""
Crypto screener agent: fetches daily market data, scores assets,
tracks realized vs potential returns over time using SQLite.

Usage:
    python crypto_agent.py fetch       # Daily: fetch data, score, save picks
    python crypto_agent.py evaluate    # Compute realized returns for past picks
    python crypto_agent.py report      # Today's picks + realized perf summary
    python crypto_agent.py backtest    # Walk-forward equity curve

Recommended cron: run `fetch` and `evaluate` once per day, same time UTC.
    0 13 * * * cd /path && python crypto_agent.py fetch && python crypto_agent.py evaluate

Data source: CoinGecko free public API (no key required, but rate limited).
"""

import argparse
import json
import math
import os
import sqlite3
import statistics
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

# ---------- Configuration ----------
# DB path is env-driven so cron + webhook services on Railway can share a
# single volume (e.g. CRYPTO_AGENT_DB=/data/crypto_agent.db). Defaults to
# the local working dir for dev.
DB_PATH = Path(os.environ.get("CRYPTO_AGENT_DB", "crypto_agent.db"))
COINGECKO_API = "https://api.coingecko.com/api/v3"
# Optional free Demo API key from coingecko.com — raises rate limit from ~10 to 30 req/min.
# Set COINGECKO_API_KEY in Railway env vars (Settings → Variables).
COINGECKO_API_KEY = os.environ.get("COINGECKO_API_KEY", "")
TOP_N_UNIVERSE = 250          # coins fetched (by market cap)
TOP_N_PICKS = 10              # picks surfaced per day
MIN_MARKET_CAP = 50_000_000   # liquidity floor: $50M
MIN_DAILY_VOLUME = 5_000_000  # liquidity floor: $5M
EXCLUDE_KEYWORDS = [
    "usd", "usdt", "usdc", "dai", "busd", "tusd", "fdusd",
    "wrapped", "staked", "liquid staked", "stake",
    # Liquid staking and wrapped ETH/BTC derivatives — they track the underlying,
    # not an independent market, so their momentum signals are redundant with BTC/ETH.
    "weth", "reth", "steth", "cbeth", "sfrxeth", "weeth", "rocket pool",
]

# Pump guard: coins that have already moved this much are likely in a
# late-stage pump — fade risk outweighs chance opportunity.
MAX_7D_CHANGE_PCT  = 60.0  # drop coins up > 60% in 7 days
MAX_24H_CHANGE_PCT = 25.0  # drop coins up > 25% in 24 hours
MAX_RS_SCORE       = 3.0   # drop coins with extreme relative-strength z-score (3σ)
EVAL_HORIZONS = [1, 3, 7]
REQUEST_DELAY = 2.5  # seconds between API calls — increased for free-tier stability

# Factor weights for the composite score.
# Loaded from weights.json (written by weight_refitter.py) when available.
# Decorrelation is injected at 10% with the remaining 5 factors renormalized to 90%.
def _load_weights() -> dict:
    defaults = {
        "momentum":      0.315,
        "volume":        0.180,
        "volatility":    0.135,
        "reversal":      0.135,
        "rel_strength":  0.135,
        "decorrelation": 0.100,
    }
    # Check the volume path (written by weight_refitter.py on Railway) first,
    # then fall back to the app-directory copy shipped with the repo.
    volume_path = DB_PATH.parent / "weights.json"
    app_path    = Path(__file__).parent / "weights.json"
    weights_path = volume_path if volume_path.exists() else app_path
    if not weights_path.exists():
        return defaults
    try:
        with open(weights_path) as f:
            data = json.load(f)
        w = dict(data.get("weights", {}))
        if not w:
            return defaults
        if "decorrelation" not in w:
            scale = 0.90 / max(sum(w.values()), 1e-9)
            w = {k: v * scale for k, v in w.items()}
            w["decorrelation"] = 0.10
        required = set(defaults.keys())
        if not required.issubset(w.keys()):
            return defaults
        if abs(sum(w.values()) - 1.0) > 0.05:
            return defaults
        print(f"[weights] Loaded from weights.json (generated {data.get('generated_at', '?')[:10]})",
              flush=True)
        return w
    except Exception as e:
        print(f"[weights] Could not load weights.json ({e}) — using defaults", flush=True)
        return defaults

WEIGHTS = _load_weights()


def reload_weights() -> dict:
    """Re-read weights.json so Sunday refits apply without process restart."""
    global WEIGHTS
    WEIGHTS = _load_weights()
    return WEIGHTS


# ---------- Database ----------
def init_db():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    # WAL mode is required when multiple processes share this DB (Railway:
    # cron service + webhook service both write). Default rollback journal
    # has writer-blocks-reader semantics; WAL allows concurrent readers
    # while a single writer is active. Persists in the file header — only
    # needs to be set once per DB, but idempotent so harmless on reruns.
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS snapshots (
        snapshot_date TEXT,
        coin_id TEXT,
        symbol TEXT,
        name TEXT,
        price REAL,
        market_cap REAL,
        volume_24h REAL,
        change_24h REAL,
        change_7d REAL,
        change_30d REAL,
        ath_change_pct REAL,
        atl_change_pct REAL,
        PRIMARY KEY (snapshot_date, coin_id)
    );

    CREATE TABLE IF NOT EXISTS picks (
        pick_date TEXT,
        rank INTEGER,
        coin_id TEXT,
        symbol TEXT,
        entry_price REAL,
        composite_score REAL,
        momentum_score REAL,
        volume_score REAL,
        volatility_score REAL,
        reversal_score REAL,
        rs_score REAL,
        realized_1d REAL,
        realized_3d REAL,
        realized_7d REAL,
        PRIMARY KEY (pick_date, coin_id)
    );

    -- Per-coin factor scores for the FULL eligible universe each day.
    -- Required by weight_refitter.py: composite alone loses the per-factor
    -- signal needed to refit weights against realized returns.
    CREATE TABLE IF NOT EXISTS factor_scores (
        pick_date TEXT,
        coin_id TEXT,
        symbol TEXT,
        momentum REAL,
        volume REAL,
        volatility REAL,
        reversal REAL,
        rel_strength REAL,
        decorrelation REAL,
        composite_score REAL,
        PRIMARY KEY (pick_date, coin_id)
    );

    CREATE INDEX IF NOT EXISTS idx_snap_coin ON snapshots(coin_id, snapshot_date);
    CREATE INDEX IF NOT EXISTS idx_fs_date ON factor_scores(pick_date);
    """)

    # Migrate existing DBs: add new columns if they don't exist yet.
    for table, col in [
        ("picks",        "decorrelation_score"),
        ("factor_scores","decorrelation"),
        ("factor_scores","composite_score"),
    ]:
        try:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} REAL")
        except sqlite3.OperationalError:
            pass  # column already exists

    conn.commit()
    return conn


# ---------- Data fetch ----------
def _cg_get(url, params, max_retries=4):
    """GET wrapper with exponential backoff on 429 rate-limit responses."""
    headers = {"x-cg-demo-api-key": COINGECKO_API_KEY} if COINGECKO_API_KEY else {}
    for attempt in range(max_retries):
        r = requests.get(url, params=params, headers=headers, timeout=30)
        if r.status_code == 429:
            wait = 60 * (attempt + 1)  # 60s → 120s → 180s → 240s
            print(f"  [CoinGecko] rate limited — waiting {wait}s (attempt {attempt+1}/{max_retries})", flush=True)
            time.sleep(wait)
            continue
        r.raise_for_status()
        return r
    raise Exception("CoinGecko rate limit persisted after max retries")


def fetch_top_coins(limit=TOP_N_UNIVERSE):
    coins = []
    per_page = 250
    pages = (limit + per_page - 1) // per_page
    for page in range(1, pages + 1):
        url = f"{COINGECKO_API}/coins/markets"
        params = {
            "vs_currency": "usd",
            "order": "market_cap_desc",
            "per_page": per_page,
            "page": page,
            "price_change_percentage": "24h,7d,30d",
        }
        r = _cg_get(url, params)
        coins.extend(r.json())
        time.sleep(REQUEST_DELAY)
    return coins[:limit]


def is_excluded(coin):
    name = (coin.get("name") or "").lower()
    sym = (coin.get("symbol") or "").lower()
    if any(kw in name or kw == sym for kw in EXCLUDE_KEYWORDS):
        return True
    # Price-stability filter: exclude near-$1 tokens with <3% 30d move.
    # Catches rebasing/pegged tokens that don't have "usd" in their name (e.g. RUSD).
    price = coin.get("current_price") or 0
    change_30d = abs(coin.get("price_change_percentage_30d_in_currency") or 0)
    if 0.90 <= price <= 1.10 and change_30d < 3.0:
        return True
    return False


# ---------- BTC correlation ----------
def compute_btc_correlations(conn, coin_ids: list) -> dict:
    """Return {coin_id: 30d pearson correlation with BTC} for all coin_ids.
    Single batch query — runs in O(coins) not O(coins²). Returns 0.0 for
    coins with < 10 days of overlap with BTC snapshots."""
    cur    = conn.cursor()
    cutoff = (datetime.now(timezone.utc) - timedelta(days=32)).date().isoformat()

    cur.execute("""
        SELECT snapshot_date, price FROM snapshots
        WHERE coin_id = 'bitcoin' AND snapshot_date >= ?
        ORDER BY snapshot_date ASC
    """, (cutoff,))
    btc_rows = cur.fetchall()
    btc_prices = {r[0]: r[1] for r in btc_rows if r[1]}
    btc_dates  = sorted(btc_prices.keys())
    if len(btc_dates) < 5:   # need ≥ 4 return pairs; gracefully absent on new deploys
        return {}
    btc_returns = {
        btc_dates[i]: btc_prices[btc_dates[i]] / btc_prices[btc_dates[i - 1]] - 1
        for i in range(1, len(btc_dates))
    }

    if not coin_ids:
        return {}
    placeholders = ",".join("?" for _ in coin_ids)
    cur.execute(f"""
        SELECT coin_id, snapshot_date, price FROM snapshots
        WHERE coin_id IN ({placeholders}) AND snapshot_date >= ?
        ORDER BY coin_id, snapshot_date ASC
    """, (*coin_ids, cutoff))

    coin_prices: dict = {}
    for coin_id, snap_date, price in cur.fetchall():
        if price:
            coin_prices.setdefault(coin_id, {})[snap_date] = price

    correlations = {}
    for coin_id in coin_ids:
        prices = coin_prices.get(coin_id, {})
        s_dates = sorted(prices.keys())
        if len(s_dates) < 5:   # not enough history for this coin yet
            correlations[coin_id] = 0.0
            continue
        a = [prices[s_dates[i]] / prices[s_dates[i - 1]] - 1 for i in range(1, len(s_dates))]
        b = [btc_returns.get(s_dates[i], None) for i in range(1, len(s_dates))]
        pairs = [(ai, bi) for ai, bi in zip(a, b) if bi is not None]
        if len(pairs) < 4:   # need ≥ 4 overlapping return days
            correlations[coin_id] = 0.0
            continue
        a2, b2 = [p[0] for p in pairs], [p[1] for p in pairs]
        ma, mb  = sum(a2) / len(a2), sum(b2) / len(b2)
        cov     = sum((ai - ma) * (bi - mb) for ai, bi in zip(a2, b2))
        var_a   = sum((ai - ma) ** 2 for ai in a2)
        var_b   = sum((bi - mb) ** 2 for bi in b2)
        denom   = (var_a * var_b) ** 0.5
        correlations[coin_id] = cov / denom if denom > 0 else 0.0

    return correlations


# ---------- Signals ----------
def zscore(values, x):
    if len(values) < 2:
        return 0.0
    mu = statistics.mean(values)
    sd = statistics.pstdev(values)
    return 0.0 if sd == 0 else (x - mu) / sd


def compute_signals(coins, btc_change_7d, correlations=None):
    eligible = [
        c for c in coins
        if c.get("market_cap") and c["market_cap"] >= MIN_MARKET_CAP
        and c.get("total_volume") and c["total_volume"] >= MIN_DAILY_VOLUME
        and not is_excluded(c)
        and c.get("price_change_percentage_24h_in_currency") is not None
        and abs(c.get("price_change_percentage_7d_in_currency") or 0) <= MAX_7D_CHANGE_PCT
        and abs(c.get("price_change_percentage_24h_in_currency") or 0) <= MAX_24H_CHANGE_PCT
        and (c.get("symbol") or "").upper().isascii()  # skip non-ASCII tickers
    ]

    momentum_7d    = [c.get("price_change_percentage_7d_in_currency")  or 0 for c in eligible]
    momentum_30d   = [c.get("price_change_percentage_30d_in_currency") or 0 for c in eligible]
    change_24h     = [c.get("price_change_percentage_24h_in_currency") or 0 for c in eligible]
    abs_change_24h = [abs(x) for x in change_24h]
    vol_ratio      = [c["total_volume"] / c["market_cap"] for c in eligible]
    # Log-transform ATL distance before z-scoring: raw values span 100% to 4,700,000%
    # (new coins vs BTC), making the distribution so skewed that all picks cluster at z≈0.
    atl_dist_log   = [math.log1p(max(0, c.get("atl_change_percentage") or 0)) for c in eligible]
    # BTC correlation for decorrelation factor — lower ABSOLUTE corr → higher score.
    # Use abs() so high inverse correlation (-0.9) is treated the same as high positive
    # correlation (+0.9): both coins are driven by BTC, just in opposite directions.
    corr_vals      = [abs(correlations.get(c["id"], 0.0)) if correlations else 0.0 for c in eligible]
    # Cross-sectional relative-strength: excess 7d return vs BTC, z-scored so it
    # sits on the same scale as every other factor (mean 0, std ~1).
    rs_vals        = [m7 - btc_change_7d for m7 in momentum_7d]

    scored = []
    for c in eligible:
        m7      = c.get("price_change_percentage_7d_in_currency") or 0
        m30     = c.get("price_change_percentage_30d_in_currency") or 0
        m24     = c.get("price_change_percentage_24h_in_currency") or 0
        vr      = c["total_volume"] / c["market_cap"]
        atl_log = math.log1p(max(0, c.get("atl_change_percentage") or 0))
        corr    = abs(correlations.get(c["id"], 0.0)) if correlations else 0.0

        # 30d lookback is academically optimal for cross-sectional crypto momentum
        # (Starkiller Capital 2022, SSRN 4675565). Blended 24h+7d+30d reduces
        # sensitivity to single-day spikes while capturing persistent trends.
        momentum      = (0.2 * zscore(change_24h, m24)
                       + 0.4 * zscore(momentum_7d, m7)
                       + 0.4 * zscore(momentum_30d, m30))
        volume        = zscore(vol_ratio, vr)
        volatility    = zscore(abs_change_24h, abs(m24))
        reversal      = -zscore(atl_dist_log, atl_log)  # closer to ATL = more recovery room
        rs            = zscore(rs_vals, m7 - btc_change_7d)
        decorrelation = -zscore(corr_vals, corr)         # lower BTC corr = higher score

        composite = (
            WEIGHTS["momentum"]       * momentum +
            WEIGHTS["volume"]         * volume +
            WEIGHTS["volatility"]     * volatility +
            WEIGHTS["reversal"]       * reversal +
            WEIGHTS["rel_strength"]   * rs +
            WEIGHTS.get("decorrelation", 0.0) * decorrelation
        )

        scored.append({
            "coin_id":             c["id"],
            "symbol":              c["symbol"].upper(),
            "name":                c["name"],
            "price":               c["current_price"],
            "change_24h":          m24,
            "change_7d":           m7,
            "composite_score":     composite,
            "momentum_score":      momentum,
            "volume_score":        volume,
            "volatility_score":    volatility,
            "reversal_score":      reversal,
            "rs_score":            rs,
            "decorrelation_score": decorrelation,
        })

    # Drop extreme RS outliers — these are late-stage pumps, not early entries
    scored = [s for s in scored if abs(s["rs_score"]) <= MAX_RS_SCORE]
    scored.sort(key=lambda x: x["composite_score"], reverse=True)
    return scored


# ---------- Commands ----------
def cmd_fetch(conn):
    reload_weights()  # pick up Sunday refit without requiring a redeploy
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    print(f"[{today}] Fetching universe of top {TOP_N_UNIVERSE}...")
    coins = fetch_top_coins(TOP_N_UNIVERSE)
    print(f"  fetched {len(coins)} coins")

    btc = next((c for c in coins if c["id"] == "bitcoin"), None)
    btc_7d = (btc.get("price_change_percentage_7d_in_currency") if btc else 0) or 0

    cur = conn.cursor()
    for c in coins:
        if not c.get("market_cap"):
            continue
        cur.execute("""
            INSERT OR REPLACE INTO snapshots VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
        """, (
            today, c["id"], (c.get("symbol") or "").upper(), c.get("name"),
            c.get("current_price"), c.get("market_cap"), c.get("total_volume"),
            c.get("price_change_percentage_24h_in_currency"),
            c.get("price_change_percentage_7d_in_currency"),
            c.get("price_change_percentage_30d_in_currency"),
            c.get("ath_change_percentage"), c.get("atl_change_percentage"),
        ))
    conn.commit()  # commit snapshots before querying them for correlations

    # Compute BTC correlations from historical snapshots (now that today's are written)
    coin_ids     = [c["id"] for c in coins if c.get("market_cap")]
    correlations = compute_btc_correlations(conn, coin_ids)
    print(f"  correlation data available for {len(correlations)} coins", flush=True)

    scored = compute_signals(coins, btc_7d, correlations)

    # Persist per-coin factor scores for the full eligible universe.
    # weight_refitter.py needs this to learn factor → realized-return mapping.
    # Top 10 alone is too small AND survivorship-biased (we'd only see the picks).
    for s in scored:
        cur.execute("""
            INSERT OR REPLACE INTO factor_scores
            (pick_date, coin_id, symbol, momentum, volume, volatility,
             reversal, rel_strength, decorrelation, composite_score)
            VALUES (?,?,?,?,?,?,?,?,?,?)
        """, (
            today, s["coin_id"], s["symbol"],
            s["momentum_score"], s["volume_score"], s["volatility_score"],
            s["reversal_score"], s["rs_score"], s["decorrelation_score"],
            s["composite_score"],
        ))

    picks = scored[:TOP_N_PICKS]

    # Clear today's picks before inserting fresh ones so re-runs don't leave
    # stale entries from previous fetches (e.g. coins filtered by pump guard).
    cur.execute("DELETE FROM picks WHERE pick_date = ?", (today,))

    for rank, p in enumerate(picks, 1):
        cur.execute("""
            INSERT OR REPLACE INTO picks
            (pick_date, rank, coin_id, symbol, entry_price, composite_score,
             momentum_score, volume_score, volatility_score, reversal_score, rs_score,
             decorrelation_score, realized_1d, realized_3d, realized_7d)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (
            today, rank, p["coin_id"], p["symbol"], p["price"], p["composite_score"],
            p["momentum_score"], p["volume_score"], p["volatility_score"],
            p["reversal_score"], p["rs_score"], p["decorrelation_score"],
            None, None, None,
        ))
    conn.commit()

    print(f"\nTop {TOP_N_PICKS} picks for {today}:")
    print(f"{'#':<4}{'SYMBOL':<10}{'SCORE':<10}{'PRICE':<14}{'24h%':<10}{'7d%':<10}")
    for i, p in enumerate(picks, 1):
        print(f"{i:<4}{p['symbol']:<10}{p['composite_score']:<+10.2f}"
              f"${p['price']:<13.6g}{p['change_24h']:<+10.2f}{p['change_7d']:<+10.2f}")


def cmd_evaluate(conn):
    """Look up future-day snapshots and fill in realized returns for past picks."""
    cur = conn.cursor()
    cur.execute("""
        SELECT pick_date, coin_id, entry_price, realized_1d, realized_3d, realized_7d
        FROM picks
    """)
    rows = cur.fetchall()
    today = datetime.now(timezone.utc).date()

    updates = 0
    for pick_date, coin_id, entry, r1, r3, r7 in rows:
        pick_dt = datetime.strptime(pick_date, "%Y-%m-%d").date()
        days_passed = (today - pick_dt).days

        for horizon, current in [(1, r1), (3, r3), (7, r7)]:
            if current is not None or days_passed < horizon or not entry:
                continue
            target = (pick_dt + timedelta(days=horizon)).strftime("%Y-%m-%d")
            cur.execute(
                "SELECT price FROM snapshots WHERE coin_id = ? AND snapshot_date >= ?"
                " ORDER BY snapshot_date ASC LIMIT 1",
                (coin_id, target),
            )
            row = cur.fetchone()
            if not row or not row[0]:
                continue
            ret = (row[0] / entry - 1) * 100
            cur.execute(
                f"UPDATE picks SET realized_{horizon}d = ? WHERE pick_date = ? AND coin_id = ?",
                (ret, pick_date, coin_id),
            )
            updates += 1

    conn.commit()
    print(f"Filled in {updates} realized-return cells.")

    # Coverage check: if CoinGecko remaps or delists a coin_id, the JOIN to
    # snapshots silently produces NULLs and our validation loop quietly degrades.
    # Flag picks where days_passed >= horizon but realized_Xd is still NULL.
    print("\n=== Coverage check ===")
    coverage_ok = True
    for horizon in EVAL_HORIZONS:
        cur.execute(f"""
            SELECT pick_date, coin_id, symbol
            FROM picks
            WHERE realized_{horizon}d IS NULL
              AND julianday(?) - julianday(pick_date) >= ?
        """, (today.strftime("%Y-%m-%d"), horizon))
        missing = cur.fetchall()
        cur.execute(f"""
            SELECT COUNT(*) FROM picks
            WHERE julianday(?) - julianday(pick_date) >= ?
        """, (today.strftime("%Y-%m-%d"), horizon))
        eligible_n = cur.fetchone()[0] or 0
        if eligible_n == 0:
            print(f"  {horizon}d: no picks aged in yet")
            continue
        filled = eligible_n - len(missing)
        cov = 100.0 * filled / eligible_n
        flag = " " if cov >= 95.0 else " ⚠ below 95%"
        print(f"  {horizon}d: {filled}/{eligible_n} filled ({cov:.1f}%){flag}")
        if cov < 95.0:
            coverage_ok = False
            # Show the first few offenders so you can investigate (likely
            # CoinGecko remap, delist, or snapshot fetch failure on target date).
            for pd_, cid, sym in missing[:5]:
                print(f"      missing: {pd_} {sym} ({cid})")
            if len(missing) > 5:
                print(f"      ... and {len(missing) - 5} more")
    if not coverage_ok:
        print("  → investigate before trusting realized-return stats.")


def cmd_report(conn):
    cur = conn.cursor()

    cur.execute("SELECT MAX(pick_date) FROM picks")
    latest = cur.fetchone()[0]
    if latest:
        print(f"=== Latest picks ({latest}) ===")
        cur.execute("""
            SELECT rank, symbol, composite_score, entry_price,
                   momentum_score, volume_score, volatility_score, reversal_score, rs_score
            FROM picks WHERE pick_date = ? ORDER BY rank
        """, (latest,))
        for rank, sym, score, price, mom, vol, vlt, rev, rs in cur.fetchall():
            print(f"  #{rank:<2} {sym:<8} score={score:+5.2f} @ ${price:<12.6g} "
                  f"[mom={mom:+.2f} vol={vol:+.2f} vlt={vlt:+.2f} rev={rev:+.2f} rs={rs:+.2f}]")

    print("\n=== Realized vs potential (system performance) ===")
    for h in EVAL_HORIZONS:
        col = f"realized_{h}d"
        cur.execute(f"""
            SELECT AVG({col}),
                   100.0 * SUM(CASE WHEN {col} > 0 THEN 1.0 ELSE 0.0 END) / COUNT({col}),
                   COUNT({col})
            FROM picks WHERE {col} IS NOT NULL
        """)
        avg, hit, n = cur.fetchone()
        if n:
            print(f"  {h}d horizon: n={n:<4} avg_return={avg:+6.2f}%  hit_rate={hit:5.1f}%")
        else:
            print(f"  {h}d horizon: not enough history yet")

    print("\n=== BTC baseline (same days) ===")
    for h in EVAL_HORIZONS:
        cur.execute(f"""
            SELECT AVG((s2.price / s1.price - 1) * 100)
            FROM (SELECT DISTINCT pick_date FROM picks WHERE realized_{h}d IS NOT NULL) p
            JOIN snapshots s1 ON s1.coin_id='bitcoin' AND s1.snapshot_date = p.pick_date
            JOIN snapshots s2 ON s2.coin_id='bitcoin'
                AND s2.snapshot_date = date(p.pick_date, '+{h} days')
        """)
        result = cur.fetchone()[0]
        if result is not None:
            print(f"  {h}d BTC avg: {result:+6.2f}%")
        else:
            print(f"  {h}d BTC: not enough history yet")

    print("\nIf 'system avg' beats 'BTC avg' over many samples, the screener is adding value.")


def cmd_backtest(conn):
    """Equity curve from realized 3d returns of daily top-10 baskets."""
    cur = conn.cursor()
    cur.execute("""
        SELECT pick_date, AVG(realized_3d)
        FROM picks WHERE realized_3d IS NOT NULL
        GROUP BY pick_date ORDER BY pick_date
    """)
    rows = cur.fetchall()
    if not rows:
        print("Need at least 3 days of evaluated picks. Try again later.")
        return
    print(f"3d realized returns over {len(rows)} pick-dates:")
    eq = 1.0
    for date, avg_ret in rows:
        eq *= 1 + (avg_ret or 0) / 100
        print(f"  {date}: avg_3d={avg_ret:+6.2f}%   equity={eq:.4f}x")


# ---------- Strong signal detection ----------
def detect_strong_signals(conn, date: str) -> list:
    """Check today's picks for three exceptional conditions:
    1. Composite score >= strong_signal threshold (exceptional momentum)
    2. Volume score >= surge threshold (unusual buying activity)
    3. Price within X% of all-time high (breakout candidate)
    """
    cfg_path = Path(__file__).parent / "config.json"
    ss_cfg = {}
    if cfg_path.exists():
        with open(cfg_path) as f:
            ss_cfg = json.load(f).get("strong_signals", {})

    score_thresh  = ss_cfg.get("score_exceptional", 2.5)
    vol_thresh    = ss_cfg.get("volume_surge_score", 3.0)
    ath_within    = ss_cfg.get("ath_within_pct", 20.0)

    cur = conn.cursor()
    cur.execute("""
        SELECT p.symbol, p.composite_score, p.volume_score,
               p.momentum_score, p.rs_score, p.entry_price,
               s.ath_change_pct
        FROM picks p
        LEFT JOIN snapshots s ON s.coin_id = p.coin_id AND s.snapshot_date = p.pick_date
        WHERE p.pick_date = ?
        ORDER BY p.rank
    """, (date,))

    # Only alert on coins actually tradeable on the configured exchange
    try:
        from tv_integration import get_tradeable_pairs
        tradeable = get_tradeable_pairs("US_EXCHANGES", "USDT")
    except Exception:
        tradeable = set()

    signals = []
    seen = set()

    for sym, score, vol_sc, mom_sc, rs_sc, price, ath_pct in cur.fetchall():
        if tradeable and sym.upper() not in tradeable:
            continue
        key = (sym, "score")
        if score >= score_thresh and key not in seen:
            seen.add(key)
            signals.append({
                "symbol": sym, "emoji": "🔥",
                "signal_type": "exceptional_score",
                "detail": (f"Composite score **{score:.2f}** — "
                           f"momentum {mom_sc:+.2f}, relative strength {rs_sc:+.2f}"),
            })

        key = (sym, "volume")
        if vol_sc is not None and vol_sc >= vol_thresh and key not in seen:
            seen.add(key)
            signals.append({
                "symbol": sym, "emoji": "📊",
                "signal_type": "volume_surge",
                "detail": (f"Volume score **{vol_sc:.1f}σ** above average — "
                           "unusual buying activity, watch for continuation"),
            })

        key = (sym, "ath")
        if ath_pct is not None and ath_pct >= -ath_within and key not in seen:
            seen.add(key)
            signals.append({
                "symbol": sym, "emoji": "🏔️",
                "signal_type": "near_ath",
                "detail": (f"Only **{abs(ath_pct):.1f}%** below all-time high @ ${price:.4g} — "
                           "breakout candidate if momentum holds"),
            })

    return signals


# ---------- Daily orchestration ----------
def cmd_daily(conn):
    """Full daily run: fetch → evaluate → notify Discord + email health report.
    Designed to be the single cron command. Any step failure sends a Discord
    alert and is re-raised so Railway marks the cron job as failed.
    """
    from notifier import notify_daily_picks, notify_cron_failure, send_health_report
    import sqlite3 as _sqlite3

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    warnings = []

    # 1. Fetch
    try:
        cmd_fetch(conn)
    except Exception as e:
        msg = f"fetch failed: {e}"
        notify_cron_failure("fetch", msg)
        raise

    # 2. Evaluate realized returns
    try:
        cmd_evaluate(conn)
    except Exception as e:
        warnings.append(f"evaluate step failed: {e}")
        notify_cron_failure("evaluate", str(e))

    # 3. Pull today's picks for notifications
    cur = conn.cursor()
    cur.execute("""
        SELECT rank, symbol, composite_score, entry_price
        FROM picks WHERE pick_date = ? ORDER BY rank
    """, (today,))
    all_picks = [
        {"rank": r, "symbol": s, "composite_score": sc, "entry_price": pr}
        for r, s, sc, pr in cur.fetchall()
    ]

    if not all_picks:
        warnings.append("No picks returned today — CoinGecko may be rate-limiting.")
        notify_cron_failure("picks empty", "No picks in DB after fetch.")

    # Filter to coins actually tradeable on US exchanges for Discord/email
    tradeable: set = set()
    try:
        from tv_integration import get_tradeable_pairs
        tradeable = get_tradeable_pairs("US_EXCHANGES", "USDT")
        picks = [p for p in all_picks if p["symbol"].upper() in tradeable] if tradeable else all_picks
        if len(picks) < len(all_picks):
            dropped = len(all_picks) - len(picks)
            print(f"[daily] Filtered {dropped} non-US-tradeable picks from Discord summary.", flush=True)
    except Exception:
        picks = all_picks

    # 4. Coverage check
    coverage = {"1d": 0.0, "3d": 0.0, "7d": 0.0}
    try:
        for h in [1, 3, 7]:
            cur.execute(f"""
                SELECT
                    100.0 * SUM(CASE WHEN realized_{h}d IS NOT NULL THEN 1.0 ELSE 0.0 END)
                        / MAX(COUNT(*), 1)
                FROM picks
                WHERE julianday(?) - julianday(pick_date) >= ?
            """, (today, h))
            row = cur.fetchone()
            coverage[f"{h}d"] = round(row[0] or 0.0, 1)
            if coverage[f"{h}d"] < 90 and coverage[f"{h}d"] > 0:
                warnings.append(f"{h}d realized return coverage is {coverage[f'{h}d']:.0f}% — below 90%")
    except Exception as e:
        print(f"[daily] Coverage check failed (non-fatal): {e}", flush=True)

    # 5. Paper trade stats
    paper_stats = {"open": 0, "total_closed": 0, "hit_rate": 0.0, "avg_pnl": 0.0,
                   "rolling_7d_pnl": None}
    try:
        cur.execute("SELECT COUNT(*) FROM paper_trades WHERE status='open'")
        paper_stats["open"] = cur.fetchone()[0] or 0
        cur.execute("""
            SELECT COUNT(*),
                   100.0 * SUM(CASE WHEN status='target' THEN 1.0 ELSE 0.0 END) / MAX(COUNT(*),1),
                   AVG(pnl_pct)
            FROM paper_trades WHERE status != 'open'
        """)
        row = cur.fetchone()
        if row and row[0]:
            paper_stats["total_closed"] = row[0]
            paper_stats["hit_rate"]     = round(row[1] or 0.0, 1)
            paper_stats["avg_pnl"]      = round(row[2] or 0.0, 2)
        cutoff_7d = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
        cur.execute("""
            SELECT SUM(pnl_pct * size_pct / 100) FROM paper_trades
            WHERE status != 'open' AND closed_at >= ?
        """, (cutoff_7d,))
        pnl_7d = cur.fetchone()[0]
        if pnl_7d is not None:
            paper_stats["rolling_7d_pnl"] = round(pnl_7d, 2)
    except Exception:
        pass

    # 6. Strong signal detection
    try:
        from notifier import notify_strong_signals
        strong = detect_strong_signals(conn, today)
        if strong:
            notify_strong_signals(strong, today)
            print(f"[daily] {len(strong)} strong signal(s) detected.", flush=True)
    except Exception as e:
        print(f"[daily] Strong signal check failed: {e}", flush=True)

    # 7. Generate watchlist file + Discord daily picks summary
    watchlist_symbols = []
    try:
        from tv_integration import export_watchlist, get_pine_score_string
        export_watchlist(date=today, exchange="BINANCE", filter_exchange="BINANCE_US")
        # Collect the TV symbols for the Discord message (Binance.US only — Coinbase-only
        # coins are excluded because they get BINANCE: prefix which TradingView can't resolve)
        from tv_integration import latest_picks, coingecko_to_tv_symbol, get_tradeable_pairs, SYMBOL_OVERRIDES, EXCHANGE_QUOTE
        _, rows = latest_picks(today)
        tradeable = get_tradeable_pairs("BINANCE_US", "USDT")
        for coin_id, sym, score, price in rows:
            base = SYMBOL_OVERRIDES.get(coin_id, sym).upper()
            if not tradeable or base in tradeable:
                watchlist_symbols.append(coingecko_to_tv_symbol(coin_id, sym, "BINANCE"))
        print(f"[daily] Watchlist file written with {len(watchlist_symbols)} symbols.", flush=True)
    except Exception as e:
        print(f"[daily] Watchlist generation failed: {e}", flush=True)

    news_by_symbol = {}
    try:
        from news_fetcher import fetch_picks_news
        symbols = [p["symbol"] for p in picks[:10]]
        news_by_symbol = fetch_picks_news(symbols, hours=24, max_per_coin=2)
        if news_by_symbol:
            print(f"[daily] News fetched for {len(news_by_symbol)} picks.", flush=True)
    except Exception as e:
        print(f"[daily] News fetch failed (non-fatal): {e}", flush=True)

    # Bottom-5 scored coins from today's full universe — short watch candidates.
    # Filters out stablecoins, ineligible coins, and non-US-tradeable assets.
    short_watch = []
    try:
        cur.execute("""
            SELECT symbol, composite_score FROM factor_scores
            WHERE pick_date = ? AND composite_score IS NOT NULL
            ORDER BY composite_score ASC LIMIT 20
        """, (today,))
        all_short = [{"symbol": r[0], "score": round(r[1], 2)} for r in cur.fetchall()]
        if tradeable:
            all_short = [s for s in all_short if s["symbol"].upper() in tradeable]
        short_watch = all_short[:5]
    except Exception as e:
        print(f"[daily] Short watch query failed: {e}", flush=True)

    try:
        notify_daily_picks(picks, today, warnings, watchlist_symbols=watchlist_symbols,
                           news_by_symbol=news_by_symbol, paper_stats=paper_stats,
                           short_watch=short_watch)
    except Exception as e:
        print(f"[daily] Discord picks notify failed: {e}", flush=True)

    # 8. Email health report
    try:
        send_health_report(
            date=today,
            picks=picks,
            paper_stats=paper_stats,
            coverage=coverage,
            warnings=warnings if warnings else None,
        )
    except Exception as e:
        print(f"[daily] Email report failed: {e}", flush=True)

    print(f"[daily] Done. {len(picks)} picks, {len(warnings)} warnings.", flush=True)


# ---------- CLI ----------
def main():
    p = argparse.ArgumentParser(description="Crypto screener agent")
    p.add_argument("command", choices=["fetch", "evaluate", "report", "backtest", "daily"])
    args = p.parse_args()

    conn = init_db()
    try:
        {
            "fetch":    cmd_fetch,
            "evaluate": cmd_evaluate,
            "report":   cmd_report,
            "backtest": cmd_backtest,
            "daily":    cmd_daily,
        }[args.command](conn)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
