#!/usr/bin/env python3
"""
Flask receiver for TradingView webhook alerts.
- Validates auth token
- Cross-references with screener picks (via ai_trader)
- Opens paper trades on approved decisions

Setup:
    pip install flask
    export WEBHOOK_AUTH_TOKEN="your-long-random-string"
    python webhook_server.py            # listens on :8080

For TradingView to reach this in production, expose with ngrok / Cloudflare Tunnel,
or deploy to a VPS / Render / Fly.io. Note: TradingView webhooks require a
paid plan (Essential or higher).

In TradingView:
    Create alert → set Webhook URL → http://<your-host>:8080/webhook
    Message → leave Pine Script's auto-generated JSON
"""

import json
import os
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

from flask import Flask, jsonify, request
from apscheduler.schedulers.background import BackgroundScheduler

import ai_trader

app = Flask(__name__)
AUTH_TOKEN = os.environ.get("WEBHOOK_AUTH_TOKEN", "CHANGE_ME")
USE_LLM = os.environ.get("USE_LLM_DECIDER", "false").lower() == "true"
ANTHROPIC_KEY = os.environ.get("ANTHROPIC_API_KEY")

try:
    ai_trader.init_trading_tables().close()
    print("DB init OK", flush=True)
except Exception as e:
    print(f"DB init warning: {e} — tables will be created on first request", flush=True)


# ── Background scheduler ────────────────────────────────────────────────────

def _run_daily():
    """Runs in a background thread — fetch picks, evaluate, notify Discord + email."""
    print("[scheduler] Starting daily run...", flush=True)
    try:
        import crypto_agent
        conn = crypto_agent.init_db()
        try:
            crypto_agent.cmd_daily(conn)
        finally:
            conn.close()
    except Exception as e:
        print(f"[scheduler] Daily run failed: {e}", flush=True)
        try:
            from notifier import notify_cron_failure
            notify_cron_failure("daily screener", str(e))
        except Exception:
            pass


def _run_live_eval():
    """Every-4h live-price trade evaluation — sends Discord close cards intraday."""
    try:
        closed = ai_trader.evaluate_open_trades_live()
        if closed:
            print(f"[scheduler] Live eval closed {closed} trade(s).", flush=True)
    except Exception as e:
        print(f"[scheduler] Live eval failed: {e}", flush=True)


def _run_refit():
    """Weekly walk-forward weight refit — runs Sundays 14:00 UTC."""
    print("[scheduler] Starting weekly refit...", flush=True)
    try:
        import weight_refitter
        if weight_refitter.schema_ok():
            weight_refitter.refit_and_write()
        else:
            print("[scheduler] Refit skipped — not enough data yet.", flush=True)
    except Exception as e:
        print(f"[scheduler] Refit failed: {e}", flush=True)


def _picks_exist_today() -> bool:
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    try:
        db = Path(os.environ.get("CRYPTO_AGENT_DB", "crypto_agent.db"))
        conn = sqlite3.connect(db)
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM picks WHERE pick_date = ?", (today,))
        count = cur.fetchone()[0]
        conn.close()
        return count > 0
    except Exception:
        return False


def start_scheduler():
    scheduler = BackgroundScheduler(timezone="UTC")
    # Daily screener: 1pm UTC every day
    scheduler.add_job(_run_daily, "cron", hour=13, minute=0, id="daily")
    # Weekly refit: 2pm UTC every Sunday
    scheduler.add_job(_run_refit, "cron", day_of_week="sun", hour=14, minute=0, id="refit")
    # Live trade evaluation: every 4h at :15 past each 4H bar close (0,4,8,12,16,20 UTC)
    scheduler.add_job(_run_live_eval, "cron", hour="0,4,8,12,16,20", minute=15, id="live_eval")
    scheduler.start()
    print("[scheduler] Started — daily@13:00 UTC, live-eval@every 4h, refit@Sunday 14:00 UTC",
          flush=True)

    # Catch-up: if it's past 1pm UTC and no picks yet today, run immediately
    now = datetime.now(timezone.utc)
    if now.hour >= 13 and not _picks_exist_today():
        print("[scheduler] No picks yet today — running catch-up fetch now.", flush=True)
        threading.Thread(target=_run_daily, daemon=True).start()

    return scheduler


@app.route("/", methods=["GET"])
def health():
    return {"status": "ok", "service": "crypto-screener-webhook",
            "decider": "llm" if USE_LLM else "rules"}


@app.route("/webhook", methods=["POST"])
def webhook():
    # TradingView sends raw JSON or text
    data = request.get_json(force=True, silent=True)
    if not data and request.data:
        try:
            data = json.loads(request.data)
        except Exception:
            return jsonify({"error": "invalid JSON"}), 400
    if not data:
        return jsonify({"error": "empty payload"}), 400

    if data.get("auth") != AUTH_TOKEN:
        return jsonify({"error": "unauthorized"}), 401

    alert_id = ai_trader.log_alert(data)

    try:
        if USE_LLM and ANTHROPIC_KEY:
            decision = ai_trader.decide_trade_with_llm(data, ANTHROPIC_KEY)
        else:
            decision = ai_trader.decide_trade(data)
    except Exception as e:
        return jsonify({"alert_id": alert_id, "error": f"decider failed: {e}"}), 500

    # Persist decider output regardless of action — passes are signal too.
    ai_trader.log_decision(alert_id, decision)

    trade_id = None
    if decision.action == "enter":
        trade_id = ai_trader.open_paper_trade(decision, alert_id)

    print(f"[alert {alert_id}] {data.get('symbol')}: "
          f"{decision.action.upper()} - {decision.reasoning}")

    return jsonify({
        "alert_id": alert_id,
        "trade_id": trade_id,
        "decision": json.loads(decision.to_json()),
    })


@app.route("/picks", methods=["GET"])
def get_picks():
    """Return today's screener picks as JSON. Used by automation agents."""
    if request.args.get("auth") != AUTH_TOKEN:
        return jsonify({"error": "unauthorized"}), 401
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    try:
        db = Path(os.environ.get("CRYPTO_AGENT_DB", "crypto_agent.db"))
        conn = sqlite3.connect(db)
        cur = conn.cursor()
        cur.execute("""
            SELECT rank, symbol, composite_score, entry_price, pick_date
            FROM picks WHERE pick_date = ? ORDER BY rank
        """, (today,))
        picks = [{"rank": r, "symbol": s, "score": round(sc, 3), "price": p, "date": d}
                 for r, s, sc, p, d in cur.fetchall()]
        conn.close()
        return jsonify({"date": today, "count": len(picks), "picks": picks})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/evaluate", methods=["POST"])
def run_evaluate():
    """Trigger paper trade evaluation. mode=live uses current Binance prices (default);
    mode=snapshots uses daily CoinGecko snapshots."""
    data = request.get_json(force=True, silent=True) or {}
    if data.get("auth") != AUTH_TOKEN:
        return jsonify({"error": "unauthorized"}), 401
    try:
        if data.get("mode") == "snapshots":
            result = ai_trader.evaluate_open_trades()
        else:
            result = ai_trader.evaluate_open_trades_live()
        return jsonify({"status": "ok", "closed": result})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/status", methods=["GET"])
def system_status():
    """System health: picks freshness, open trades, recent alert activity."""
    if request.args.get("auth") != AUTH_TOKEN:
        return jsonify({"error": "unauthorized"}), 401
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    try:
        db = Path(os.environ.get("CRYPTO_AGENT_DB", "crypto_agent.db"))
        conn = sqlite3.connect(db)
        cur = conn.cursor()
        cur.execute("SELECT MAX(pick_date), COUNT(*) FROM picks WHERE pick_date = ?", (today,))
        row = cur.fetchone()
        picks_today = row[1] or 0

        cur.execute("SELECT COUNT(*) FROM picks WHERE pick_date = date(?, '-1 day')", (today,))
        picks_yesterday = cur.fetchone()[0] or 0

        open_trades = 0
        recent_alerts = 0
        try:
            cur.execute("SELECT COUNT(*) FROM paper_trades WHERE status='open'")
            open_trades = cur.fetchone()[0] or 0
            cur.execute("""
                SELECT COUNT(*) FROM alerts
                WHERE received_at >= datetime('now', '-72 hours')
            """)
            recent_alerts = cur.fetchone()[0] or 0
        except Exception:
            pass
        conn.close()
        return jsonify({
            "date": today,
            "picks_today": picks_today,
            "picks_yesterday": picks_yesterday,
            "open_trades": open_trades,
            "recent_alerts_72h": recent_alerts,
            "picks_fresh": picks_today > 0,
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


def _mask_secret(s: str) -> str:
    """Show 'configured (****abcd)' or '***unset/default***' — never the full value."""
    if not s or s == "CHANGE_ME":
        return "*** UNSET — set WEBHOOK_AUTH_TOKEN before exposing publicly ***"
    return f"configured (****{s[-4:]})" if len(s) >= 4 else "configured (short)"


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    print(f"Webhook server starting on :{port}")
    print(f"Auth token: {_mask_secret(AUTH_TOKEN)}")
    print(f"Anthropic key: {'configured' if ANTHROPIC_KEY else 'not set'}")
    print(f"Decider: {'Claude LLM' if USE_LLM and ANTHROPIC_KEY else 'rules-based'}")
    start_scheduler()
    app.run(host="0.0.0.0", port=port, debug=False)
