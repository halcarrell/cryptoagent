#!/usr/bin/env python3
"""
Portfolio risk monitor + shadow trade ledger.

Two jobs:
  1. Pre-trade circuit breaker — fills the correlation/concentration gap in
     decide_trade() by enforcing portfolio-level rules before paper trades open.
  2. Shadow trade ledger — every blocked entry is recorded as a "shadow" trade
     (same entry/stop/target as the real one would have been) and tracked the
     same way. Lets you validate the risk monitor itself: if shadow returns
     consistently beat real paper returns, the rules are too aggressive. Same
     feedback-loop pattern that snapshots-of-all-250 give the screener weights.

Pre-trade checks (all must pass for entry):
  1. Total committed exposure stays under MAX_TOTAL_EXPOSURE
  2. Open trade count stays under MAX_OPEN_TRADES
  3. New position's 30d return correlation with each open position
     stays below MAX_CORRELATION
  4. 7-day rolling weighted P&L not in circuit-breaker territory
  5. Consecutive loss streak below LOSS_STREAK_PAUSE

Usage:
    python risk_monitor.py status     # current portfolio state
    python risk_monitor.py report     # status + recent rejection log
    python risk_monitor.py evaluate   # live-price eval of open shadow trades
    python risk_monitor.py compare    # shadow-vs-paper performance comparison

Programmatic:
    from risk_monitor import check_pre_trade_risk, log_rejection
    approved, reason = check_pre_trade_risk(decision)
    if not approved:
        log_rejection(alert_id, decision, reason)  # also opens shadow trade
"""

import json
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional, Tuple

DB_PATH = Path("crypto_agent.db")

# --- Risk parameters: the agent's "instructions". Tune to your tolerance. ---
MAX_TOTAL_EXPOSURE  = 20.0
MAX_OPEN_TRADES     = 6
MAX_CORRELATION     = 0.75
CB_LOSS_THRESHOLD   = -8.0
LOSS_STREAK_PAUSE   = 5
CORR_LOOKBACK_DAYS  = 30


# ----- DB -----
def init_risk_tables():
    conn = sqlite3.connect(DB_PATH)
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS risk_rejections (
            rejection_id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT,
            alert_id INTEGER,
            symbol TEXT,
            reason TEXT,
            decision_payload TEXT
        );
        CREATE TABLE IF NOT EXISTS shadow_trades (
            shadow_id INTEGER PRIMARY KEY AUTOINCREMENT,
            rejection_id INTEGER,
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
            rejection_reason TEXT,
            status TEXT,           -- 'open' | 'stopped' | 'target'
            exit_price REAL,
            pnl_pct REAL
        );
    """)
    conn.commit()
    return conn


# ----- Helpers -----
def _strip_quote(symbol: str) -> str:
    for q in ("USDT", "USDC", "USD", "BUSD"):
        if symbol.upper().endswith(q):
            return symbol[: -len(q)].upper()
    return symbol.upper()


def _correlation(a: list, b: list) -> Optional[float]:
    if len(a) != len(b) or len(a) < 10:
        return None
    n = len(a)
    mean_a, mean_b = sum(a) / n, sum(b) / n
    cov   = sum((a[i] - mean_a) * (b[i] - mean_b) for i in range(n))
    var_a = sum((x - mean_a) ** 2 for x in a)
    var_b = sum((x - mean_b) ** 2 for x in b)
    denom = (var_a * var_b) ** 0.5
    return cov / denom if denom > 0 else None


def _daily_returns(symbol_base: str, lookback_days: int = CORR_LOOKBACK_DAYS) -> list:
    conn = sqlite3.connect(DB_PATH)
    cur  = conn.cursor()
    cutoff = (datetime.now(timezone.utc) - timedelta(days=lookback_days + 1)).date().isoformat()
    cur.execute("""
        SELECT snapshot_date, price FROM snapshots
        WHERE symbol = ? AND snapshot_date >= ?
        ORDER BY snapshot_date ASC
    """, (symbol_base, cutoff))
    rows = cur.fetchall()
    conn.close()
    prices = [r[1] for r in rows if r[1] is not None]
    if len(prices) < 2:
        return []
    return [(prices[i] / prices[i - 1] - 1) for i in range(1, len(prices))]


# ----- Portfolio state -----
def get_portfolio_state() -> dict:
    conn = sqlite3.connect(DB_PATH)
    cur  = conn.cursor()

    cur.execute("""
        SELECT trade_id, symbol, side, size_pct, entry_price, opened_at
        FROM paper_trades WHERE status = 'open'
    """)
    open_trades = [
        dict(zip(["trade_id", "symbol", "side", "size_pct", "entry_price", "opened_at"], r))
        for r in cur.fetchall()
    ]
    total_exposure = sum(t["size_pct"] for t in open_trades)

    cutoff = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
    cur.execute("""
        SELECT SUM(pnl_pct * size_pct / 100) FROM paper_trades
        WHERE status != 'open' AND closed_at >= ?
    """, (cutoff,))
    rolling_7d = cur.fetchone()[0] or 0.0

    cur.execute("""
        SELECT pnl_pct FROM paper_trades
        WHERE status != 'open' ORDER BY closed_at DESC LIMIT 10
    """)
    recent = [r[0] for r in cur.fetchall()]
    streak = 0
    for pnl in recent:
        if pnl is not None and pnl < 0:
            streak += 1
        else:
            break

    conn.close()
    return {
        "open_trades":    open_trades,
        "open_count":     len(open_trades),
        "total_exposure": round(total_exposure, 2),
        "rolling_7d_pnl": round(rolling_7d, 2),
        "loss_streak":    streak,
    }


# ----- The agent's main entry point -----
def check_pre_trade_risk(decision) -> Tuple[bool, str]:
    if decision.action != "enter":
        return True, "not an entry — risk monitor passes"

    state    = get_portfolio_state()
    new_base = _strip_quote(decision.symbol)

    projected = state["total_exposure"] + (decision.size_pct or 0)
    if projected > MAX_TOTAL_EXPOSURE:
        return False, f"projected exposure {projected:.1f}% exceeds {MAX_TOTAL_EXPOSURE}% ceiling"

    if state["open_count"] >= MAX_OPEN_TRADES:
        return False, f"{state['open_count']} open trades, max is {MAX_OPEN_TRADES}"

    new_returns = _daily_returns(new_base)
    if new_returns:
        for t in state["open_trades"]:
            existing_base = _strip_quote(t["symbol"])
            if existing_base == new_base:
                return False, f"already long {existing_base} — no doubling up"
            existing_returns = _daily_returns(existing_base)
            n = min(len(new_returns), len(existing_returns))
            if n >= 10:
                rho = _correlation(new_returns[-n:], existing_returns[-n:])
                if rho is not None and abs(rho) > MAX_CORRELATION:
                    return False, (f"30d correlation {rho:.2f} with open "
                                   f"{existing_base} exceeds {MAX_CORRELATION}")

    if state["rolling_7d_pnl"] < CB_LOSS_THRESHOLD:
        return False, (f"7d weighted P&L {state['rolling_7d_pnl']:.2f}% below "
                       f"circuit breaker {CB_LOSS_THRESHOLD}%; cooling off")

    if state["loss_streak"] >= LOSS_STREAK_PAUSE:
        return False, f"{state['loss_streak']} consecutive losses; mandatory review"

    return True, (f"approved: exposure {projected:.1f}/{MAX_TOTAL_EXPOSURE}, "
                  f"trades {state['open_count'] + 1}/{MAX_OPEN_TRADES}, "
                  f"7d P&L {state['rolling_7d_pnl']:+.2f}%")


# ----- Rejection logging + shadow trade opening -----
def log_rejection(alert_id: Optional[int], decision, reason: str):
    """Persist blocked-trade context AND open a shadow trade so we can later
    validate whether the rejection was correct. Webhook integration unchanged
    — the new return tuple is additive."""
    conn = init_risk_tables()
    cur  = conn.cursor()
    payload = decision.to_json() if hasattr(decision, "to_json") else json.dumps(str(decision))
    now = datetime.now(timezone.utc).isoformat()

    cur.execute("""
        INSERT INTO risk_rejections (timestamp, alert_id, symbol, reason, decision_payload)
        VALUES (?, ?, ?, ?, ?)
    """, (now, alert_id, decision.symbol, reason, payload))
    rejection_id = cur.lastrowid

    shadow_id = None
    if (decision.action == "enter"
            and decision.entry and decision.stop and decision.target):
        cur.execute("""
            INSERT INTO shadow_trades
                (rejection_id, alert_id, opened_at, symbol, side,
                 entry_price, stop_price, target_price,
                 size_pct, confidence, rejection_reason, status)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'open')
        """, (
            rejection_id, alert_id, now, decision.symbol, decision.side,
            decision.entry, decision.stop, decision.target,
            decision.size_pct, decision.confidence, reason,
        ))
        shadow_id = cur.lastrowid

    conn.commit()
    conn.close()
    return rejection_id, shadow_id


# ----- Shadow trade evaluation -----
def evaluate_shadow_trades_live():
    """Mark open shadow trades stopped/target against live exchange prices.
    Reuses ai_trader.fetch_live_price so both ledgers see the same prices."""
    from ai_trader import fetch_live_price

    conn = init_risk_tables()
    cur  = conn.cursor()
    cur.execute("""
        SELECT s.*, a.exchange FROM shadow_trades s
        LEFT JOIN alerts a ON s.alert_id = a.alert_id
        WHERE s.status = 'open'
    """)
    cols = [d[0] for d in cur.description]
    rows = [dict(zip(cols, r)) for r in cur.fetchall()]

    closed = 0
    for t in rows:
        last_price = fetch_live_price(t["symbol"], t.get("exchange") or "binance")
        if last_price is None:
            continue
        side, entry = t["side"], t["entry_price"]
        status, exit_p = None, None
        if side == "long":
            if last_price <= t["stop_price"]:
                status, exit_p = "stopped", t["stop_price"]
            elif last_price >= t["target_price"]:
                status, exit_p = "target", t["target_price"]
        else:
            if last_price >= t["stop_price"]:
                status, exit_p = "stopped", t["stop_price"]
            elif last_price <= t["target_price"]:
                status, exit_p = "target", t["target_price"]
        if status:
            pnl = (exit_p / entry - 1) * 100 if side == "long" else (entry / exit_p - 1) * 100
            cur.execute("""
                UPDATE shadow_trades SET status=?, closed_at=?, exit_price=?, pnl_pct=?
                WHERE shadow_id=?
            """, (status, datetime.now(timezone.utc).isoformat(), exit_p, pnl, t["shadow_id"]))
            closed += 1

    conn.commit()
    conn.close()
    print(f"Closed {closed} shadow trades.")
    return closed


# ----- Reporting -----
def report_status():
    state = get_portfolio_state()
    print("=== Portfolio risk state ===")
    print(f"Open trades:     {state['open_count']}/{MAX_OPEN_TRADES}")
    print(f"Total exposure:  {state['total_exposure']:.2f}%/{MAX_TOTAL_EXPOSURE}%")
    print(f"7d weighted P&L: {state['rolling_7d_pnl']:+.2f}% (CB at {CB_LOSS_THRESHOLD}%)")
    print(f"Loss streak:     {state['loss_streak']} (pause at {LOSS_STREAK_PAUSE})")
    if state["open_trades"]:
        print("\nOpen positions:")
        for t in state["open_trades"]:
            print(f"  {t['symbol']:<14} {t['side']:<6} {t['size_pct']:>5.2f}%  "
                  f"@ {t['entry_price']}  opened {t['opened_at'][:10]}")


def report_rejections(limit: int = 20):
    conn = init_risk_tables()
    cur  = conn.cursor()
    cur.execute("""
        SELECT timestamp, symbol, reason FROM risk_rejections
        ORDER BY rejection_id DESC LIMIT ?
    """, (limit,))
    rows = cur.fetchall()
    conn.close()
    print(f"\n=== Last {limit} risk rejections ===")
    if not rows:
        print("None yet.")
        return
    for ts, sym, reason in rows:
        print(f"{ts[:19]}  {sym:<14}  {reason}")


def _summarize_ledger(conn, table: str, days: int) -> dict:
    """Return aggregate stats for either paper_trades or shadow_trades."""
    cur = conn.cursor()
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    cur.execute(f"""
        SELECT COUNT(*),
               SUM(CASE WHEN pnl_pct > 0 THEN 1 ELSE 0 END),
               AVG(pnl_pct),
               SUM(pnl_pct * size_pct / 100)
        FROM {table}
        WHERE status != 'open' AND closed_at >= ?
    """, (cutoff,))
    n, wins, avg, weighted = cur.fetchone()
    n = n or 0
    return {
        "n":            n,
        "hit_rate":     (wins / n * 100) if n else 0.0,
        "avg_pnl":      avg or 0.0,
        "weighted_pnl": weighted or 0.0,
    }


def compare_shadow_vs_paper(days: int = 30):
    """Validate the risk monitor itself: were the blocked trades actually
    losers? If shadow weighted P&L > paper weighted P&L, rules are too tight
    — we're filtering out winners. If consistently lower, rules are working."""
    conn   = init_risk_tables()
    paper  = _summarize_ledger(conn, "paper_trades",  days)
    shadow = _summarize_ledger(conn, "shadow_trades", days)

    print(f"=== Shadow vs paper, last {days} days ===")
    print(f"{'':<10}{'Trades':>8}{'Hit %':>10}{'Avg %':>10}{'Wtd %':>10}")
    print(f"{'Paper':<10}{paper['n']:>8}{paper['hit_rate']:>9.1f}%"
          f"{paper['avg_pnl']:>+9.2f}%{paper['weighted_pnl']:>+9.2f}%")
    print(f"{'Shadow':<10}{shadow['n']:>8}{shadow['hit_rate']:>9.1f}%"
          f"{shadow['avg_pnl']:>+9.2f}%{shadow['weighted_pnl']:>+9.2f}%")

    # Per-rule breakdown — which rejection reasons cost us money?
    cur = conn.cursor()
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    cur.execute("""
        SELECT
            CASE
                WHEN rejection_reason LIKE '%exposure%'           THEN 'exposure_cap'
                WHEN rejection_reason LIKE '%open trades%'        THEN 'trade_count'
                WHEN rejection_reason LIKE '%correlation%'        THEN 'correlation'
                WHEN rejection_reason LIKE '%doubling up%'        THEN 'duplicate'
                WHEN rejection_reason LIKE '%circuit breaker%'    THEN 'cb_loss'
                WHEN rejection_reason LIKE '%consecutive losses%' THEN 'streak'
                ELSE 'other'
            END AS rule,
            COUNT(*),
            SUM(CASE WHEN pnl_pct > 0 THEN 1 ELSE 0 END),
            AVG(pnl_pct),
            SUM(pnl_pct * size_pct / 100)
        FROM shadow_trades
        WHERE status != 'open' AND closed_at >= ?
        GROUP BY rule
        ORDER BY 5 DESC
    """, (cutoff,))
    breakdown = cur.fetchall()
    conn.close()

    if breakdown:
        print(f"\nBlocked-trade outcomes by rule (positive Wtd % = rule was costly):")
        print(f"{'Rule':<14}{'N':>6}{'Hit %':>10}{'Avg %':>10}{'Wtd %':>10}")
        for rule, n, wins, avg, weighted in breakdown:
            n = n or 0
            hr = (wins / n * 100) if n else 0
            print(f"{rule:<14}{n:>6}{hr:>9.1f}%"
                  f"{(avg or 0):>+9.2f}%{(weighted or 0):>+9.2f}%")

    # Verdict
    print()
    diff = shadow["weighted_pnl"] - paper["weighted_pnl"]
    if shadow["n"] < 10:
        print(f"⚠ Only {shadow['n']} closed shadow trades — keep collecting data.")
    elif diff > 1.0:
        print(f"⚠ Shadow outperforms paper by {diff:+.2f}% — rules may be too tight.")
    elif diff < -1.0:
        print(f"✓ Shadow underperforms paper by {-diff:.2f}% — rules are protecting capital.")
    else:
        print(f"≈ Shadow and paper within {abs(diff):.2f}% — near-neutral; need more data.")


if __name__ == "__main__":
    valid = ("status", "report", "evaluate", "compare")
    if len(sys.argv) < 2 or sys.argv[1] not in valid:
        print(f"Usage: python risk_monitor.py [{'|'.join(valid)}]")
        sys.exit(1)
    init_risk_tables()
    if sys.argv[1] == "status":
        report_status()
    elif sys.argv[1] == "report":
        report_status()
        report_rejections()
    elif sys.argv[1] == "evaluate":
        evaluate_shadow_trades_live()
    else:
        compare_shadow_vs_paper()
