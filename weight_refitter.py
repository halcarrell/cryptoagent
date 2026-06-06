#!/usr/bin/env python3
"""
Walk-forward composite score weight refitter.

Asks the data which factors actually predicted forward returns. Outputs
proposed new weights with walk-forward validation comparing the fitted
weights to current weights on held-out data.

Does NOT auto-apply weights. Writes them to weights.json for review. To
apply, either (a) hand-edit crypto_agent.py with the new values, or (b)
have crypto_agent.py read weights.json on startup as the source of truth.

Schema requirements (see init_check() if missing):
  factor_scores(pick_date, coin_id, symbol, momentum, volume,
                volatility, reversal, rel_strength)
  snapshots — must already exist; this script reads it.

Usage:
    python weight_refitter.py status     # data sufficiency check
    python weight_refitter.py refit      # train on recent window, write weights.json
    python weight_refitter.py validate   # walk-forward backtest of fitted vs current
"""

import json
import os
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

DB_PATH      = Path(os.environ.get("CRYPTO_AGENT_DB", "crypto_agent.db"))
WEIGHTS_PATH = Path(os.environ.get("CRYPTO_AGENT_DB", "crypto_agent.db")).parent / "weights.json"

FACTORS = ["momentum", "volume", "volatility", "reversal", "rel_strength"]
CURRENT_WEIGHTS = {
    "momentum":     0.35,
    "volume":       0.20,
    "volatility":   0.15,
    "reversal":     0.15,
    "rel_strength": 0.15,
}

MIN_OBS_FOR_REFIT = 100   # below this, refusing to fit
FORWARD_DAYS      = 3     # holding period for "realized return"
WALK_TRAIN_DAYS   = 45    # walk-forward train window
WALK_TEST_DAYS    = 15    # walk-forward test window
MAX_WEIGHT_DELTA  = 0.10  # max change per factor per refit (smoothing)


# ----- Schema check -----
def schema_ok() -> bool:
    conn = sqlite3.connect(DB_PATH)
    cur  = conn.cursor()
    cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='factor_scores'")
    if not cur.fetchone():
        conn.close()
        return False
    cur.execute("PRAGMA table_info(factor_scores)")
    cols = {row[1] for row in cur.fetchall()}
    conn.close()
    required = {"pick_date", "coin_id", "symbol"} | set(FACTORS)
    return required.issubset(cols)


def print_schema_help():
    print("factor_scores table not found or missing columns.\n")
    print("1) Create the table:")
    print("""
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
    """)
    print("2) Modify crypto_agent.py fetch to write a row per coin per day,")
    print("   storing each factor's contribution BEFORE the composite is computed.")
    print("   Without this, there's no way to refit weights — composite alone")
    print("   loses the per-factor signal we need.\n")
    print("Run this script again once factor_scores has 45+ days of data.")


# ----- Dataset construction -----
def build_dataset(start_date: str = None, end_date: str = None,
                  forward_days: int = FORWARD_DAYS) -> list:
    """Pull (factors, forward_return) tuples for all coin-days with both."""
    conn = sqlite3.connect(DB_PATH)
    cur  = conn.cursor()

    where, params = [], []
    if start_date:
        where.append("pick_date >= ?")
        params.append(start_date)
    if end_date:
        where.append("pick_date <= ?")
        params.append(end_date)
    where_clause = "WHERE " + " AND ".join(where) if where else ""

    cur.execute(f"""
        SELECT pick_date, coin_id, symbol, momentum, volume, volatility,
               reversal, rel_strength
        FROM factor_scores {where_clause}
        ORDER BY pick_date, coin_id
    """, params)
    rows = cur.fetchall()

    dataset = []
    for date, coin_id, symbol, mom, vol, volat, rev, rs in rows:
        cur.execute("""
            SELECT price FROM snapshots
            WHERE symbol = ? AND snapshot_date = ?
        """, (symbol, date))
        entry = cur.fetchone()
        if not entry or not entry[0]:
            continue

        target_date = (datetime.fromisoformat(date) + timedelta(days=forward_days)).date().isoformat()
        cur.execute("""
            SELECT price FROM snapshots
            WHERE symbol = ? AND snapshot_date >= ?
            ORDER BY snapshot_date ASC LIMIT 1
        """, (symbol, target_date))
        exit_row = cur.fetchone()
        if not exit_row or not exit_row[0]:
            continue

        ret = (exit_row[0] / entry[0] - 1)
        dataset.append({
            "pick_date": date,
            "coin_id":   coin_id,
            "factors":   [mom, vol, volat, rev, rs],
            "return":    ret,
        })

    conn.close()
    return dataset


# ----- Fitting -----
def fit_weights(dataset: list) -> dict:
    """Closed-form: weights proportional to Cov(factor_i, forward_return).
    Negative covariances clipped to 0 (we don't trade against our own factors).
    Renormalized to sum to 1."""
    if len(dataset) < MIN_OBS_FOR_REFIT:
        raise ValueError(f"Need >= {MIN_OBS_FOR_REFIT} observations, got {len(dataset)}")

    n            = len(dataset)
    factors_cols = list(zip(*[d["factors"] for d in dataset]))  # 5 columns
    returns      = [d["return"] for d in dataset]
    mean_ret     = sum(returns) / n

    covs = []
    for f_vals in factors_cols:
        mean_f = sum(f_vals) / n
        cov    = sum((f_vals[i] - mean_f) * (returns[i] - mean_ret) for i in range(n)) / n
        covs.append(cov)

    raw   = [max(0, c) for c in covs]
    total = sum(raw)
    if total == 0:
        return dict(CURRENT_WEIGHTS)  # nothing correlated positively; keep current
    return {f: round(raw[i] / total, 4) for i, f in enumerate(FACTORS)}


def smooth_weights(new: dict, current: dict, max_delta: float = MAX_WEIGHT_DELTA) -> dict:
    """Clamp each factor's change to max_delta; renormalize to sum to 1."""
    smoothed = {}
    for f in FACTORS:
        delta      = new[f] - current[f]
        smoothed[f] = current[f] + max(-max_delta, min(max_delta, delta))
    total = sum(smoothed.values())
    return {f: round(smoothed[f] / total, 4) for f in FACTORS}


def correlation_with_returns(weights: dict, dataset: list) -> float:
    """Pearson correlation of the composite score (under given weights) with
    realized forward returns. Higher = the score is more predictive."""
    n = len(dataset)
    if n < 10:
        return 0.0
    composite = [sum(weights[FACTORS[j]] * d["factors"][j] for j in range(5)) for d in dataset]
    returns   = [d["return"] for d in dataset]
    mean_c, mean_r = sum(composite) / n, sum(returns) / n
    cov   = sum((composite[i] - mean_c) * (returns[i] - mean_r) for i in range(n))
    var_c = sum((c - mean_c) ** 2 for c in composite)
    var_r = sum((r - mean_r) ** 2 for r in returns)
    denom = (var_c * var_r) ** 0.5
    return cov / denom if denom > 0 else 0.0


# ----- Walk-forward validation -----
def walk_forward_validate():
    """Sliding non-overlapping windows. Train on N days, fit weights, test on
    next M days, compare fitted vs current correlation with returns. The
    only honest way to know if refitting actually helps."""
    conn = sqlite3.connect(DB_PATH)
    cur  = conn.cursor()
    cur.execute("SELECT MIN(pick_date), MAX(pick_date) FROM factor_scores")
    min_d, max_d = cur.fetchone()
    conn.close()

    if not min_d:
        print("No factor scores yet.")
        return
    start = datetime.fromisoformat(min_d).date()
    end   = datetime.fromisoformat(max_d).date()
    span  = (end - start).days
    if span < WALK_TRAIN_DAYS + WALK_TEST_DAYS:
        need = WALK_TRAIN_DAYS + WALK_TEST_DAYS - span
        print(f"Need {WALK_TRAIN_DAYS + WALK_TEST_DAYS} days; have {span}. {need} more days needed.")
        return

    print(f"Walk-forward: train={WALK_TRAIN_DAYS}d, test={WALK_TEST_DAYS}d, "
          f"forward return={FORWARD_DAYS}d")
    print(f"\n{'Test start':<14}{'N train':>9}{'N test':>8}"
          f"{'Curr corr':>12}{'New corr':>12}{'Δ':>10}")

    cursor = start + timedelta(days=WALK_TRAIN_DAYS)
    deltas = []
    while cursor + timedelta(days=WALK_TEST_DAYS) <= end:
        train = build_dataset((cursor - timedelta(days=WALK_TRAIN_DAYS)).isoformat(),
                              cursor.isoformat())
        test  = build_dataset(cursor.isoformat(),
                              (cursor + timedelta(days=WALK_TEST_DAYS)).isoformat())

        if len(train) < MIN_OBS_FOR_REFIT or len(test) < 20:
            cursor += timedelta(days=WALK_TEST_DAYS)
            continue

        new_w     = fit_weights(train)
        smoothed  = smooth_weights(new_w, CURRENT_WEIGHTS)
        curr_corr = correlation_with_returns(CURRENT_WEIGHTS, test)
        new_corr  = correlation_with_returns(smoothed, test)
        delta     = new_corr - curr_corr
        deltas.append(delta)

        print(f"{cursor.isoformat():<14}{len(train):>9}{len(test):>8}"
              f"{curr_corr:>+12.4f}{new_corr:>+12.4f}{delta:>+10.4f}")

        cursor += timedelta(days=WALK_TEST_DAYS)

    if not deltas:
        print("\nNo windows had sufficient data.")
        return
    avg = sum(deltas) / len(deltas)
    wins = sum(1 for d in deltas if d > 0)
    print(f"\nMean OOS correlation lift: {avg:+.4f}")
    print(f"Windows where new > current: {wins}/{len(deltas)}")
    if avg > 0.02:
        print("✓ Refit weights consistently improve OOS correlation. Worth applying.")
    elif avg < -0.02:
        print("⚠ Refit weights underperform OOS. Current weights are doing fine.")
    else:
        print("≈ Refit ≈ current. No strong signal to change. Keep collecting data.")


# ----- Refit & write -----
def refit_and_write():
    end   = datetime.now(timezone.utc).date()
    start = end - timedelta(days=WALK_TRAIN_DAYS)
    data  = build_dataset(start.isoformat(), end.isoformat())

    if len(data) < MIN_OBS_FOR_REFIT:
        print(f"Only {len(data)} observations available; need {MIN_OBS_FOR_REFIT}. Skipping refit.")
        return

    raw      = fit_weights(data)
    smoothed = smooth_weights(raw, CURRENT_WEIGHTS)

    print("=== Weight refit ===")
    print(f"Window: {start} → {end}, {len(data)} obs, {FORWARD_DAYS}d forward returns")
    print(f"\n{'Factor':<14}{'Current':>10}{'Raw fit':>10}{'Smoothed':>10}{'Δ':>10}")
    for f in FACTORS:
        delta = smoothed[f] - CURRENT_WEIGHTS[f]
        print(f"{f:<14}{CURRENT_WEIGHTS[f]:>10.4f}{raw[f]:>10.4f}"
              f"{smoothed[f]:>10.4f}{delta:>+10.4f}")

    payload = {
        "generated_at":   datetime.now(timezone.utc).isoformat(),
        "training_start": start.isoformat(),
        "training_end":   end.isoformat(),
        "n_observations": len(data),
        "forward_days":   FORWARD_DAYS,
        "raw_fit":        raw,
        "weights":        smoothed,
        "previous":       dict(CURRENT_WEIGHTS),
    }
    WEIGHTS_PATH.write_text(json.dumps(payload, indent=2))
    print(f"\nWritten to {WEIGHTS_PATH}.")

    # Auto-tune min_score / max_score based on live score-bucket analysis
    try:
        auto_tune_config()
    except Exception as e:
        print(f"[auto-tune] Failed (non-fatal): {e}", flush=True)

    # Discord summary
    try:
        from notifier import _discord_post
        rows = "\n".join(
            f"{f:<14} {CURRENT_WEIGHTS[f]:.3f} → {smoothed[f]:.3f}  "
            f"({'↑' if smoothed[f] > CURRENT_WEIGHTS[f] else '↓'}{abs(smoothed[f]-CURRENT_WEIGHTS[f]):.3f})"
            for f in FACTORS
        )
        _discord_post({"embeds": [{
            "title": "⚖️ Weekly weight refit complete",
            "color": 0x9B59B6,
            "description": f"```\n{rows}\n```",
            "fields": [
                {"name": "Training window", "value": f"{start} → {end}", "inline": True},
                {"name": "Observations",   "value": str(len(data)),       "inline": True},
            ],
            "footer": {"text": "Review weights.json before applying — run validate first"},
        }]})
    except Exception:
        pass


# ----- Auto-tune config -----
def auto_tune_config():
    """Analyze live score-bucket data and auto-adjust min_score / max_score in
    the config_overrides DB table.  Called automatically after every refit.

    Rules:
    - min_score = lowest labeled bucket with avg_3d > 0 AND n >= 10
    - max_score = highest labeled bucket floor where avg_3d < 0 AND n >= 5
    - Changes are not applied unless difference >= 0.1 vs current effective value
    - Posts a Discord notification summarising any changes
    """
    conn = sqlite3.connect(DB_PATH)
    cur  = conn.cursor()

    # Ensure the overrides table exists
    cur.execute("""
        CREATE TABLE IF NOT EXISTS config_overrides (
            key TEXT PRIMARY KEY,
            value TEXT,
            updated_at TEXT,
            reason TEXT
        )
    """)
    conn.commit()

    # Score-bucket analysis (same logic as /analysis endpoint)
    cur.execute("""
        SELECT
            CASE
                WHEN composite_score < 1.5 THEN '1.0-1.5'
                WHEN composite_score < 2.0 THEN '1.5-2.0'
                WHEN composite_score < 2.5 THEN '2.0-2.5'
                WHEN composite_score < 3.0 THEN '2.5-3.0'
                ELSE '3.0+'
            END AS bucket,
            COUNT(*),
            AVG(realized_3d)
        FROM picks
        WHERE realized_3d IS NOT NULL
        GROUP BY bucket
        ORDER BY MIN(composite_score)
    """)
    buckets = [{"bucket": r[0], "n": r[1], "avg_3d": r[2]} for r in cur.fetchall()]
    total_n = sum(b["n"] for b in buckets)

    if total_n < 20:
        print("[auto-tune] Not enough pick history yet — skipping.", flush=True)
        conn.close()
        return

    BOUNDS = {"1.0-1.5": 1.0, "1.5-2.0": 1.5, "2.0-2.5": 2.0, "2.5-3.0": 2.5, "3.0+": 3.0}
    ORDER  = list(BOUNDS.keys())
    bmap   = {b["bucket"]: b for b in buckets}

    # Read current effective values
    cur.execute("SELECT key, value FROM config_overrides WHERE key IN ('min_score','max_score')")
    db_vals = {k: json.loads(v) for k, v in cur.fetchall()}
    cfg_path = Path(__file__).parent / "config.json"
    cfg_risk = json.loads(cfg_path.read_text()).get("risk", {}) if cfg_path.exists() else {}
    eff_min = db_vals.get("min_score", cfg_risk.get("min_score", 1.2))
    eff_max = db_vals.get("max_score", cfg_risk.get("max_score", 2.5))

    # Recommended min: first bucket (ascending) with avg_3d > 0 and n >= 10
    new_min = eff_min
    for bk in ORDER:
        b = bmap.get(bk)
        if b and b["n"] >= 10 and (b.get("avg_3d") or 0) > 0:
            new_min = BOUNDS[bk]
            break

    # Recommended max: walk from top down; cap at first bucket with negative avg_3d
    new_max = eff_max
    for bk in reversed(ORDER):
        b = bmap.get(bk)
        if b and b["n"] >= 5 and (b.get("avg_3d") or 0) < 0:
            new_max = BOUNDS[bk]
        else:
            break

    now_iso = datetime.now(timezone.utc).isoformat()
    changes = []
    for key, current, proposed in [("min_score", eff_min, new_min),
                                    ("max_score",  eff_max, new_max)]:
        if abs(proposed - current) >= 0.1:
            reason = (f"Auto-tuned from {total_n} picks: "
                      f"bucket analysis suggests {key}={proposed:.1f}")
            cur.execute("""
                INSERT OR REPLACE INTO config_overrides (key, value, updated_at, reason)
                VALUES (?, ?, ?, ?)
            """, (key, json.dumps(proposed), now_iso, reason))
            changes.append((key, current, proposed))

    conn.commit()
    conn.close()

    if changes:
        lines = "\n".join(f"• {k}: **{c}** → **{p}**" for k, c, p in changes)
        print(f"[auto-tune] Applied: {changes}", flush=True)
        try:
            from notifier import _discord_post
            _discord_post({"embeds": [{
                "title": "⚙️ Config auto-tuned from score data",
                "color": 0x1ABC9C,
                "description": (
                    f"{lines}\n\n"
                    f"*Based on {total_n} picks. Takes effect on next signal — no redeploy needed.*"
                ),
                "footer": {"text": now_iso[:10]},
            }]})
        except Exception:
            pass
    else:
        print("[auto-tune] Config already optimal — no changes applied.", flush=True)


# ----- Status -----
def show_status():
    if not schema_ok():
        print_schema_help()
        return
    conn = sqlite3.connect(DB_PATH)
    cur  = conn.cursor()
    cur.execute("SELECT COUNT(*), MIN(pick_date), MAX(pick_date) FROM factor_scores")
    n, mn, mx = cur.fetchone()
    conn.close()
    print(f"factor_scores rows: {n}")
    if not mn:
        print("No data yet — modify crypto_agent.py to populate factor_scores on each fetch.")
        return
    span = (datetime.fromisoformat(mx).date() - datetime.fromisoformat(mn).date()).days
    print(f"Date range: {mn} → {mx} ({span} days)")
    need = WALK_TRAIN_DAYS + WALK_TEST_DAYS
    if span < need:
        print(f"Need {need - span} more days for walk-forward validation.")
    else:
        print(f"✓ Sufficient data — `refit` and `validate` are both available.")


if __name__ == "__main__":
    valid = ("status", "refit", "validate")
    if len(sys.argv) < 2 or sys.argv[1] not in valid:
        print(f"Usage: python weight_refitter.py [{'|'.join(valid)}]")
        sys.exit(1)
    if not schema_ok():
        print_schema_help()
        sys.exit(1)
    if sys.argv[1] == "status":
        show_status()
    elif sys.argv[1] == "refit":
        refit_and_write()
    else:
        walk_forward_validate()
