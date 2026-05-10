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
TOP_N_UNIVERSE = 250          # coins fetched (by market cap)
TOP_N_PICKS = 10              # picks surfaced per day
MIN_MARKET_CAP = 50_000_000   # liquidity floor: $50M
MIN_DAILY_VOLUME = 5_000_000  # liquidity floor: $5M
EXCLUDE_KEYWORDS = [
    "usd", "usdt", "usdc", "dai", "busd", "tusd", "fdusd",
    "wrapped", "staked", "liquid staked", "stake",
]
EVAL_HORIZONS = [1, 3, 7]
REQUEST_DELAY = 1.5  # seconds between API calls (free tier ~30/min)

# Factor weights for the composite score. Tune these based on backtest.
WEIGHTS = {
    "momentum":   0.35,
    "volume":     0.20,
    "volatility": 0.15,
    "reversal":   0.15,
    "rel_strength": 0.15,
}


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
        PRIMARY KEY (pick_date, coin_id)
    );

    CREATE INDEX IF NOT EXISTS idx_snap_coin ON snapshots(coin_id, snapshot_date);
    CREATE INDEX IF NOT EXISTS idx_fs_date ON factor_scores(pick_date);
    """)
    conn.commit()
    return conn


# ---------- Data fetch ----------
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
        r = requests.get(url, params=params, timeout=30)
        r.raise_for_status()
        coins.extend(r.json())
        time.sleep(REQUEST_DELAY)
    return coins[:limit]


def is_excluded(coin):
    name = (coin.get("name") or "").lower()
    sym = (coin.get("symbol") or "").lower()
    return any(kw in name or kw == sym for kw in EXCLUDE_KEYWORDS)


# ---------- Signals ----------
def zscore(values, x):
    if len(values) < 2:
        return 0.0
    mu = statistics.mean(values)
    sd = statistics.pstdev(values)
    return 0.0 if sd == 0 else (x - mu) / sd


def compute_signals(coins, btc_change_7d):
    eligible = [
        c for c in coins
        if c.get("market_cap") and c["market_cap"] >= MIN_MARKET_CAP
        and c.get("total_volume") and c["total_volume"] >= MIN_DAILY_VOLUME
        and not is_excluded(c)
        and c.get("price_change_percentage_24h_in_currency") is not None
    ]

    momentum_7d  = [c.get("price_change_percentage_7d_in_currency")  or 0 for c in eligible]
    change_24h   = [c.get("price_change_percentage_24h_in_currency") or 0 for c in eligible]
    abs_change_24h = [abs(x) for x in change_24h]
    vol_ratio    = [c["total_volume"] / c["market_cap"] for c in eligible]
    atl_dist     = [c.get("atl_change_percentage") or 0 for c in eligible]

    scored = []
    for c in eligible:
        m7  = c.get("price_change_percentage_7d_in_currency") or 0
        m24 = c.get("price_change_percentage_24h_in_currency") or 0
        vr  = c["total_volume"] / c["market_cap"]
        atl = c.get("atl_change_percentage") or 0

        momentum   = 0.5 * zscore(momentum_7d, m7) + 0.5 * zscore(change_24h, m24)
        volume     = zscore(vol_ratio, vr)
        volatility = zscore(abs_change_24h, abs(m24))
        reversal   = -zscore(atl_dist, atl)  # closer to ATL = more recovery room
        rs         = (m7 - btc_change_7d) / 10  # rough scaling

        composite = (
            WEIGHTS["momentum"]     * momentum +
            WEIGHTS["volume"]       * volume +
            WEIGHTS["volatility"]   * volatility +
            WEIGHTS["reversal"]     * reversal +
            WEIGHTS["rel_strength"] * rs
        )

        scored.append({
            "coin_id": c["id"],
            "symbol": c["symbol"].upper(),
            "name": c["name"],
            "price": c["current_price"],
            "change_24h": m24,
            "change_7d": m7,
            "composite_score": composite,
            "momentum_score": momentum,
            "volume_score": volume,
            "volatility_score": volatility,
            "reversal_score": reversal,
            "rs_score": rs,
        })

    scored.sort(key=lambda x: x["composite_score"], reverse=True)
    return scored


# ---------- Commands ----------
def cmd_fetch(conn):
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

    scored = compute_signals(coins, btc_7d)

    # Persist per-coin factor scores for the full eligible universe.
    # weight_refitter.py needs this to learn factor → realized-return mapping.
    # Top 10 alone is too small AND survivorship-biased (we'd only see the picks).
    for s in scored:
        cur.execute("""
            INSERT OR REPLACE INTO factor_scores
            (pick_date, coin_id, symbol, momentum, volume, volatility, reversal, rel_strength)
            VALUES (?,?,?,?,?,?,?,?)
        """, (
            today, s["coin_id"], s["symbol"],
            s["momentum_score"], s["volume_score"], s["volatility_score"],
            s["reversal_score"], s["rs_score"],
        ))

    picks = scored[:TOP_N_PICKS]

    for rank, p in enumerate(picks, 1):
        cur.execute("""
            INSERT OR REPLACE INTO picks
            (pick_date, rank, coin_id, symbol, entry_price, composite_score,
             momentum_score, volume_score, volatility_score, reversal_score, rs_score,
             realized_1d, realized_3d, realized_7d)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (
            today, rank, p["coin_id"], p["symbol"], p["price"], p["composite_score"],
            p["momentum_score"], p["volume_score"], p["volatility_score"],
            p["reversal_score"], p["rs_score"],
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
                "SELECT price FROM snapshots WHERE snapshot_date = ? AND coin_id = ?",
                (target, coin_id),
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
        eq *= 1 + (avg_ret or 0) / 100 / 3  # daily-step approximation
        print(f"  {date}: avg_3d={avg_ret:+6.2f}%   equity={eq:.4f}x")


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
    picks = [
        {"rank": r, "symbol": s, "composite_score": sc, "entry_price": pr}
        for r, s, sc, pr in cur.fetchall()
    ]

    if not picks:
        warnings.append("No picks returned today — CoinGecko may be rate-limiting.")
        notify_cron_failure("picks empty", "No picks in DB after fetch.")

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
    except Exception:
        pass

    # 5. Paper trade stats
    paper_stats = {"open": 0, "total_closed": 0, "hit_rate": 0.0, "avg_pnl": 0.0}
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
    except Exception:
        pass

    # 6. Discord daily picks summary
    try:
        notify_daily_picks(picks, today, warnings)
    except Exception as e:
        print(f"[daily] Discord notify failed: {e}", flush=True)

    # 7. Email health report
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
