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
from datetime import datetime, timezone, timedelta
from pathlib import Path

from flask import Flask, jsonify, request
from apscheduler.schedulers.background import BackgroundScheduler

import ai_trader

app = Flask(__name__)
AUTH_TOKEN = os.environ.get("WEBHOOK_AUTH_TOKEN", "CHANGE_ME")
USE_LLM = os.environ.get("USE_LLM_DECIDER", "false").lower() == "true"
ANTHROPIC_KEY = os.environ.get("ANTHROPIC_API_KEY")

# Epoch timestamp of the last time a streak/circuit-breaker risk alert was sent.
# Prevents Discord flood when many TV signals fire during an active loss streak.
_RISK_STREAK_ALERT_COOLDOWN = 4 * 3600  # 4 hours
_last_streak_alert_ts: float = 0.0

# Cooldown for "signal received but not traded" pass notifications.
# Regime and score-cap blocks repeat on every TV signal while the condition holds —
# we notify once per 4 hours so the user knows why they're not getting trade cards.
_PASS_NOTIFY_COOLDOWN = 4 * 3600
_last_pass_alert_ts:  float = 0.0
# Reasons worth notifying about (user fired a signal but server declined to trade)
_PASS_NOTIFY_KEYWORDS = (
    "bearish market regime", "EMA50 < EMA200",
    "above", "cap", "overextended", "bearish regime",
)

# Per-symbol+side cooldown for scanner Discord signal cards. The scanner runs
# every 15 min and the same coin can show qualifying conditions for hours —
# without this, a single persistent setup floods Discord with a card every cycle.
_SCANNER_NOTIFY_COOLDOWN = 4 * 3600  # 4 hours
_scanner_last_notified: dict = {}    # {"SYMBOL-side": epoch_ts}

try:
    ai_trader.init_trading_tables().close()
    print("DB init OK", flush=True)
except Exception as e:
    print(f"DB init warning: {e} — tables will be created on first request", flush=True)


# ── Background scheduler ────────────────────────────────────────────────────

def _compute_and_store_breadth():
    """Sample up to 40 picks from factor_scores and compute % above 20-bar 4H MA.
    Stores result in ai_trader module cache so decide_trade() can read it.
    Called once per day after picks are loaded.
    """
    try:
        import sqlite3, random
        db = Path(os.environ.get("CRYPTO_AGENT_DB", "crypto_agent.db"))
        conn = sqlite3.connect(db)
        cur  = conn.cursor()
        cur.execute("SELECT MAX(pick_date) FROM factor_scores")
        date = cur.fetchone()[0]
        if not date:
            conn.close()
            return
        cur.execute("SELECT symbol FROM factor_scores WHERE pick_date = ?", (date,))
        syms = [r[0] for r in cur.fetchall()]
        conn.close()
        if not syms:
            return
        sample = random.sample(syms, min(40, len(syms)))
        since  = datetime.now(timezone.utc) - timedelta(hours=100)  # 25 × 4H bars
        above = total = 0
        for sym in sample:
            try:
                pair = sym.upper() + "USDT" if not sym.upper().endswith("USDT") else sym.upper()
                candles = ai_trader.fetch_binance_ohlcv(pair, since, "4h")
                if len(candles) < 20:
                    continue
                ma20 = sum(c["close"] for c in candles[-20:]) / 20
                if candles[-1]["close"] > ma20:
                    above += 1
                total += 1
            except Exception:
                continue
        if total > 0:
            pct = above / total * 100
            ai_trader.store_breadth(pct)
            print(f"[breadth] {above}/{total} coins above 4H MA20 = {pct:.1f}%", flush=True)
    except Exception as e:
        print(f"[breadth] Compute failed: {e}", flush=True)


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
    # Compute breadth after picks are loaded (best-effort, non-blocking)
    try:
        import threading
        threading.Thread(target=_compute_and_store_breadth, daemon=True).start()
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


def _run_opportunity_scan():
    """Every-15-min session scanner — fires the moment entry conditions align.

    Research-backed: 13-17 UTC has peak liquidity and cleanest momentum
    (EU/US session overlap). Instead of waiting up to 4H for a TradingView
    bar close, this checks all screener picks against live Binance 1H candles
    every 15 minutes during the active window and opens paper trades immediately
    when conditions match — making TradingView optional for entries.
    Active window: 11:45-18:00 UTC (covers EU open through US session close).
    """
    now_utc = datetime.now(timezone.utc)
    if not (6 <= now_utc.hour < 23):
        return  # skip only the quietest dead hours (23:00–06:00 UTC)

    try:
        db  = Path(os.environ.get("CRYPTO_AGENT_DB", "crypto_agent.db"))
        conn = sqlite3.connect(db)
        cur  = conn.cursor()
        today = now_utc.strftime("%Y-%m-%d")

        # Top-10 picks for long + short scanning
        cur.execute("SELECT symbol, composite_score, rank FROM picks WHERE pick_date = ? ORDER BY rank", (today,))
        picks = [{"symbol": r[0], "score": r[1], "rank": r[2]} for r in cur.fetchall()]

        # Short Watch — bottom-5 by composite score (most negative = weakest = best short candidates)
        # These are the high-liquidity coins (BTC, ETH, BNB, DOGE, SHIB) the screener doesn't pick
        # as longs because they can't outperform themselves, but they're ideal shorts in RISK_OFF.
        cur.execute("""
            SELECT symbol, composite_score FROM factor_scores
            WHERE pick_date = ?
            ORDER BY composite_score ASC
            LIMIT 5
        """, (today,))
        short_watch = [{"symbol": r[0], "score": r[1], "rank": 999, "is_short_watch": True}
                       for r in cur.fetchall()]
        print(f"[scanner] picks={len(picks)} short_watch={len(short_watch)} "
              f"({today})", flush=True)

        # Coins already in open trades — skip to avoid doubling up
        cur.execute("SELECT UPPER(symbol) FROM paper_trades WHERE status='open'")
        open_syms = {r[0] for r in cur.fetchall()}
        conn.close()

        if not picks and not short_watch:
            return

        opened = 0

        def _scan_coin(pick, sides):
            """Scan one coin for the given sides ('long', 'short', or both)."""
            nonlocal opened
            sym_base = pick["symbol"].upper()
            sym_pair = sym_base + "USDT" if not sym_base.endswith("USDT") else sym_base
            if sym_pair in open_syms or sym_base in open_syms:
                return

            since   = now_utc - timedelta(hours=210)
            candles = ai_trader.fetch_binance_ohlcv(sym_pair, since, "1h")
            if len(candles) < 50:
                return

            conds = ai_trader.compute_entry_conditions(candles)
            close = conds["close"]
            atr   = conds["atr"]
            rs    = ai_trader.compute_relative_strength(sym_pair, candles)

            for side in sides:
                if side == "long" and not conds["long_ok"]:
                    continue
                if side == "short" and not conds["short_ok"]:
                    continue
                if side == "long" and conds["short_ok"]:
                    continue  # don't long a coin showing short conditions

                if side == "long":
                    stop_p   = round(close - atr * 1.5, 8)
                    target_p = round(close + atr * 3.75, 8)  # 3.75/1.5 = 2.5:1 R:R (min threshold)
                else:
                    stop_p   = round(close + atr * 1.5, 8)
                    target_p = round(close - atr * 4.0, 8)   # 4.0/1.5 = 2.67:1 R:R

                alert = {
                    "symbol": sym_pair, "exchange": "BINANCE", "side": side,
                    "entry": close, "stop": stop_p, "target": target_p,
                    "rsi": conds["rsi"], "source": "session_scan",
                    "relative_strength": rs,
                }
                alert_id = ai_trader.log_alert(alert)
                decision = ai_trader.decide_trade(alert)
                ai_trader.log_decision(alert_id, decision)

                # Post a Discord signal card — but only once per symbol+side per
                # cooldown window. A persistent setup (e.g. coin stuck overbought
                # for hours) would otherwise re-fire a card every 15-min scan.
                # Trades that actually open always notify regardless of cooldown.
                import time as _time
                notify_key = f"{sym_pair}-{side}"
                now_ts = _time.time()
                last_ts = _scanner_last_notified.get(notify_key, 0)
                should_notify = (decision.action == "enter"
                                 or now_ts - last_ts > _SCANNER_NOTIFY_COOLDOWN)
                if should_notify:
                    try:
                        from notifier import notify_signal_received
                        notify_signal_received(alert, decision, source="scanner")
                        _scanner_last_notified[notify_key] = now_ts
                    except Exception as ne:
                        print(f"[scanner] Signal card failed: {ne}", flush=True)

                if decision.action == "enter":
                    try:
                        from risk_monitor import check_pre_trade_risk, log_rejection
                        approved, risk_reason = check_pre_trade_risk(decision)
                        if approved:
                            ai_trader.open_paper_trade(decision, alert_id)
                            opened += 1
                            print(f"[scanner] Opened {side.upper()} {sym_pair} @ {close} "
                                  f"(score={pick['score']:.2f}, rsi={conds['rsi']:.1f})", flush=True)
                        else:
                            log_rejection(alert_id, decision, risk_reason)
                    except Exception as e:
                        print(f"[scanner] Risk check error {sym_pair}: {e}", flush=True)
                        ai_trader.open_paper_trade(decision, alert_id)
                        opened += 1

        # Top-10 picks: scan for both long and short
        for pick in picks:
            _scan_coin(pick, ["long", "short"])

        # Short Watch coins: short signals only (these are the market's weakest — BTC, ETH, BNB, etc.)
        regime = ai_trader.btc_regime()
        if regime in ("bear", "sideways"):  # only scan short watch in RISK_OFF/CHOP
            for pick in short_watch:
                _scan_coin(pick, ["short"])

        if opened:
            print(f"[scanner] Session scan opened {opened} trade(s).", flush=True)

    except Exception as e:
        print(f"[scanner] Opportunity scan failed: {e}", flush=True)


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


def _run_daily_test():
    """Daily self-test at 14:00 UTC — hits live endpoints and posts pass/fail to Discord."""
    import requests as _req
    from notifier import _discord_post

    base = os.environ.get("RAILWAY_PUBLIC_DOMAIN", "")
    if not base:
        print("[self-test] RAILWAY_PUBLIC_DOMAIN not set — skipping.", flush=True)
        return
    base_url = f"https://{base}"
    token    = AUTH_TOKEN
    now_str  = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    checks = [
        ("Health",    "GET",  f"{base_url}/",                        None),
        ("Picks",     "GET",  f"{base_url}/picks?auth={token}",      None),
        ("Status",    "GET",  f"{base_url}/status?auth={token}",     None),
        ("Analysis",  "GET",  f"{base_url}/analysis?auth={token}",   None),
        ("Evaluate",  "POST", f"{base_url}/evaluate",                {"auth": token, "mode": "snapshots"}),
    ]

    results, passed, failed = [], 0, 0
    for name, method, url, body in checks:
        try:
            r = (_req.post(url, json=body, timeout=10)
                 if method == "POST"
                 else _req.get(url, timeout=10))
            ok  = r.status_code in (200, 204)
            passed += ok; failed += not ok
            results.append(f"{'✅' if ok else '❌'} **{name}** — {r.status_code}")
        except Exception as e:
            failed += 1
            results.append(f"❌ **{name}** — {e}")

    color = 0x2ECC71 if failed == 0 else (0xE67E22 if failed <= 2 else 0xE74C3C)
    _discord_post({"embeds": [{
        "title": f"{'✅' if failed == 0 else '⚠' if failed <= 2 else '🔴'} Daily self-test — {passed}/{len(checks)} passed",
        "color": color,
        "description": "\n".join(results),
        "footer": {"text": now_str},
    }]})
    print(f"[self-test] {passed}/{len(checks)} passed.", flush=True)


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
    # Daily self-test: 2pm UTC every day (1 hour after screener)
    scheduler.add_job(_run_daily_test, "cron", hour=14, minute=0, id="self_test")
    # Weekly refit: 2pm UTC every Sunday (self-test runs at :00, refit at :05)
    scheduler.add_job(_run_refit, "cron", day_of_week="sun", hour=14, minute=5, id="refit")
    # Live trade evaluation: every 4h at :15 past each 4H bar close (0,4,8,12,16,20 UTC)
    scheduler.add_job(_run_live_eval, "cron", hour="0,4,8,12,16,20", minute=15, id="live_eval")
    # Opportunity scanner: every 15 min during active trading hours (06-22 UTC).
    # Offset 2 min from :00 so scanner never collides with TradingView 4H bar-close alerts
    # (which fire at exactly :00). The 2-min gap lets TradingView's POST arrive and clear
    # the Flask thread before the scanner's Binance OHLCV calls compete for it.
    scheduler.add_job(_run_opportunity_scan, "cron",
                      hour="6,7,8,9,10,11,12,13,14,15,16,17,18,19,20,21,22", minute="2,17,32,47", id="opp_scan")
    scheduler.start()
    print("[scheduler] Started — daily@13:00 UTC, session-scan@every 15min 06-23 UTC, "
          "self-test@14:00 UTC, live-eval@every 4h, refit@Sunday 14:05 UTC", flush=True)

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


def _extract_payload(raw_bytes: bytes) -> dict:
    """Parse the webhook body.

    Supports two formats:
    1. Pure JSON  — legacy and LLM-generated webhooks
    2. Text + JSON — Pine Script v2+ format where the human-readable header
       (shown in TradingView mobile notifications) precedes the JSON block:
         🟢 LONG INJUSDT @ 7.10  SL -5.0%  TP +15.0%  R:R 3.0:1  RSI 45.2
         {"auth":"...","symbol":"INJUSDT",...}
    """
    import re
    text = raw_bytes.decode("utf-8", errors="replace").strip()
    # Try pure JSON first (fastest, covers legacy payloads)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    # Find the outermost {...} block — handles header + JSON on separate lines
    m = re.search(r'(\{.*\})', text, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(1))
        except json.JSONDecodeError:
            pass
    return {}


@app.route("/webhook", methods=["POST"])
def webhook():
    data = {}
    if request.data:
        data = _extract_payload(request.data)
    elif request.json:
        data = request.json
    if not data:
        return jsonify({"error": "empty or unparseable payload"}), 400

    if data.get("auth") != AUTH_TOKEN:
        return jsonify({"error": "unauthorized"}), 401

    alert_id = ai_trader.log_alert(data)

    # Compute signal age before deciding — used for Discord card freshness label
    alert_age_secs = 0
    alert_time_str = data.get("time")
    if alert_time_str:
        try:
            alert_ts = datetime.fromisoformat(alert_time_str.replace("Z", "+00:00"))
            alert_age_secs = int((datetime.now(timezone.utc) - alert_ts).total_seconds())
        except Exception:
            pass
    data["signal_age_secs"] = alert_age_secs

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
    risk_block = None
    if decision.action == "enter":
        try:
            from risk_monitor import check_pre_trade_risk, log_rejection
            approved, risk_reason = check_pre_trade_risk(decision)
            if not approved:
                log_rejection(alert_id, decision, risk_reason)
                risk_block = risk_reason
                print(f"[risk] #{alert_id} {data.get('symbol')}: BLOCKED — {risk_reason}",
                      flush=True)
                global _last_streak_alert_ts
                _streak_keywords = ("consecutive losses", "circuit breaker", "cooling off")
                is_repeat_type   = any(kw in risk_reason for kw in _streak_keywords)
                import time as _time
                now_ts  = _time.time()
                should_notify = (not is_repeat_type
                                 or now_ts - _last_streak_alert_ts > _RISK_STREAK_ALERT_COOLDOWN)
                if should_notify:
                    try:
                        from notifier import notify_risk_rejection
                        notify_risk_rejection(data.get("symbol", ""), risk_reason)
                        if is_repeat_type:
                            _last_streak_alert_ts = now_ts
                    except Exception:
                        pass
            else:
                trade_id = ai_trader.open_paper_trade(decision, alert_id)
        except Exception as e:
            print(f"[risk] Monitor error (bypassing): {e}", flush=True)
            trade_id = ai_trader.open_paper_trade(decision, alert_id)

    print(f"[alert {alert_id}] {data.get('symbol')}: "
          f"{decision.action.upper()} - {decision.reasoning}")

    # Always post a human-readable signal card to Discord so followers can act manually.
    # Pine Script now uses time_close (bar-close timestamp) so fresh signals arrive
    # <10s old. Anything beyond 15 min is a TradingView retry of an already-rejected
    # signal — posting a card for those just confuses followers with dead prices.
    if alert_age_secs < 900:
        try:
            from notifier import notify_signal_received
            notify_signal_received(data, decision, source="tradingview")
        except Exception as e:
            print(f"[notifier] Signal card failed: {e}", flush=True)

    return jsonify({
        "alert_id": alert_id,
        "trade_id": trade_id,
        "risk_block": risk_block,
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


@app.route("/notify", methods=["POST"])
def relay_notify():
    """Discord relay — remote agents POST here instead of hitting Discord directly.

    Accepts the standard Discord webhook payload shape plus an auth field:
        {"auth": "TOKEN", "embeds": [...]}
    Railway strips the auth and forwards the embeds to DISCORD_WEBHOOK_URL.
    This avoids Anthropic CCR IP addresses being rate-limited by Discord.
    """
    data = request.get_json(force=True, silent=True) or {}
    if data.get("auth") != AUTH_TOKEN:
        return jsonify({"error": "unauthorized"}), 401
    embeds = data.get("embeds")
    if not embeds:
        return jsonify({"error": "no embeds provided"}), 400
    try:
        from notifier import _discord_post
        _discord_post({"embeds": embeds})
        return jsonify({"status": "ok"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/admin/config", methods=["POST"])
def admin_set_config():
    """Update DB config_overrides directly (min_score, max_score).
    POST {"auth":"...","min_score":2.5} — invalidates the 1-hour cache immediately.
    """
    data = request.json or {}
    if data.get("auth") != AUTH_TOKEN:
        return jsonify({"error": "unauthorized"}), 401
    try:
        db   = Path(os.environ.get("CRYPTO_AGENT_DB", "crypto_agent.db"))
        conn = sqlite3.connect(db)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS config_overrides
            (key TEXT PRIMARY KEY, value TEXT)
        """)
        updates = {}
        for key in ("min_score", "max_score"):
            if key in data:
                conn.execute(
                    "INSERT OR REPLACE INTO config_overrides (key, value) VALUES (?, ?)",
                    (key, str(data[key]))
                )
                updates[key] = data[key]
        conn.commit()
        conn.close()
        # Bust the in-process 1-hour cache so next decision picks it up immediately
        import ai_trader as _at
        _at._config_override_cache = (0.0, {})
        return jsonify({"status": "ok", "updated": updates})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/admin/reset-streak", methods=["POST"])
def admin_reset_streak():
    """Reset the consecutive-loss circuit breaker.

    Writes streak_reset_after = now() to config_overrides so the risk monitor
    only counts trades closed after this point toward the streak. The historical
    losing trades are preserved in full for analysis — they're just excluded from
    the live streak counter.

    POST {"auth": "TOKEN"}
    """
    data = request.json or {}
    if data.get("auth") != AUTH_TOKEN:
        return jsonify({"error": "unauthorized"}), 401
    try:
        db   = Path(os.environ.get("CRYPTO_AGENT_DB", "crypto_agent.db"))
        conn = sqlite3.connect(db)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS config_overrides
            (key TEXT PRIMARY KEY, value TEXT)
        """)
        now_iso = datetime.now(timezone.utc).isoformat()
        conn.execute(
            "INSERT OR REPLACE INTO config_overrides (key, value) VALUES (?, ?)",
            ("streak_reset_after", now_iso)
        )
        conn.commit()
        conn.close()
        print(f"[admin] Loss-streak reset. Trades before {now_iso} excluded from counter.",
              flush=True)
        try:
            from notifier import _discord_post
            _discord_post({"embeds": [{
                "title": "🔄 Loss streak reset",
                "color": 0x3498DB,
                "description": (f"Consecutive-loss circuit breaker manually reset.\n"
                                f"Streak counter restarted from {now_iso[:16]} UTC.\n"
                                f"Historical trades preserved — excluded from live count only."),
            }]})
        except Exception:
            pass
        return jsonify({"status": "ok", "streak_reset_after": now_iso})
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


@app.route("/analysis", methods=["GET"])
def score_analysis():
    """Realized return stats bucketed by screener score + min_score recommendation.

    Lets you answer: 'what score threshold actually predicts positive returns?'
    Call: GET /analysis?auth=YOUR_TOKEN
    """
    if request.args.get("auth") != AUTH_TOKEN:
        return jsonify({"error": "unauthorized"}), 401
    try:
        db  = Path(os.environ.get("CRYPTO_AGENT_DB", "crypto_agent.db"))
        cfg_path = Path(__file__).parent / "config.json"
        import json as _json
        risk_cfg    = {}
        current_min = 1.2
        current_max = None
        if cfg_path.exists():
            with open(cfg_path) as f:
                risk_cfg    = _json.load(f).get("risk", {})
                current_min = risk_cfg.get("min_score", 1.2)
                current_max = risk_cfg.get("max_score", None)

        conn = sqlite3.connect(db)
        cur  = conn.cursor()

        # Bucket lower bounds used for recommendation logic — these are the
        # labeled boundaries, not the actual min score seen in each bucket.
        # (The DB may contain picks scored below 1.0 that leak into the first
        # bucket; we always compare against the label, not bucket_min.)
        _BUCKET_LOWER = {"1.0–1.5": 1.0, "1.5–2.0": 1.5, "2.0–2.5": 2.0,
                         "2.5–3.0": 2.5, "3.0+": 3.0}
        _BUCKET_ORDER = list(_BUCKET_LOWER.keys())

        # Score-bucketed realized returns
        cur.execute("""
            SELECT
                CASE
                    WHEN composite_score < 1.5 THEN '1.0–1.5'
                    WHEN composite_score < 2.0 THEN '1.5–2.0'
                    WHEN composite_score < 2.5 THEN '2.0–2.5'
                    WHEN composite_score < 3.0 THEN '2.5–3.0'
                    ELSE '3.0+'
                END AS bucket,
                COUNT(*)                                                          AS n,
                ROUND(AVG(realized_1d), 2)                                        AS avg_1d,
                ROUND(AVG(realized_3d), 2)                                        AS avg_3d,
                ROUND(100.0 * SUM(CASE WHEN realized_3d > 0 THEN 1.0 ELSE 0 END)
                      / MAX(COUNT(realized_3d), 1), 1)                            AS hit_rate_3d
            FROM picks
            WHERE realized_3d IS NOT NULL
            GROUP BY bucket
            ORDER BY MIN(composite_score)
        """)
        cols = [d[0] for d in cur.description]
        buckets = [dict(zip(cols, r)) for r in cur.fetchall()]

        # ── Recommendation engine ─────────────────────────────────────────────
        # Uses labeled bucket boundaries (not actual min scores, which can be
        # noisy on small datasets). Requires n >= 5 per bucket for confidence.
        bucket_map = {b["bucket"]: b for b in buckets}
        notes = []

        # 1. Find dead zones: buckets with positive avg_3d neighbors but weak
        #    themselves (hit_rate < 40% and avg_3d < 2%). Flag them as warnings.
        for bk in _BUCKET_ORDER:
            b = bucket_map.get(bk)
            if b and b["n"] >= 10 and (b.get("hit_rate_3d") or 0) < 40:
                notes.append(f"⚠ {bk} is a dead zone — only {b['hit_rate_3d']:.0f}% "
                              f"hit rate on {b['n']} picks. Avoid.")

        # 2. Detect non-monotonic tail: last bucket(s) with negative avg_3d and n>=3
        suggested_max = current_max
        for bk in reversed(_BUCKET_ORDER):
            b = bucket_map.get(bk)
            if b and b["n"] >= 3 and (b.get("avg_3d") or 0) < 0:
                suggested_max = _BUCKET_LOWER[bk]
            else:
                break  # stop at first non-negative from the top

        # 3. Find minimum score: walk DOWN from the top bucket, extending the
        #    "good" range while avg_3d > 1% AND hit_rate >= 40% AND n >= 5.
        #    Stops at the first dead zone. Picking the first ascending bucket
        #    with positive avg_3d (old logic) could recommend a min_score that
        #    reopens a dead zone flagged in step 1 above — contiguous-from-top
        #    avoids that contradiction.
        suggested_min = current_min
        _contig = []
        for bk in reversed(_BUCKET_ORDER):
            b = bucket_map.get(bk)
            if b and b["n"] >= 5 and (b.get("avg_3d") or 0) > 1.0 and (b.get("hit_rate_3d") or 0) >= 40:
                _contig.append(bk)
            else:
                break
        if _contig:
            suggested_min = _BUCKET_LOWER[_contig[-1]]

        # Build summary message
        changes = []
        if suggested_min != current_min:
            direction = "Raise" if suggested_min > current_min else "Lower"
            changes.append(f"{direction} min_score {current_min} → {suggested_min}")
        if suggested_max != current_max:
            if suggested_max:
                changes.append(f"Set max_score cap at {suggested_max} "
                                f"(scores above this average negative returns)")
            else:
                changes.append("Remove max_score cap — high-score picks performing well")

        if not buckets or sum(b["n"] for b in buckets) < 20:
            msg = "Not enough data yet — check back after 30+ days of picks."
        elif changes:
            msg = " | ".join(changes) + "."
            if notes:
                msg += " Also: " + "; ".join(notes)
        elif notes:
            msg = " ".join(notes)
        else:
            msg = (f"Config looks well-calibrated — "
                   f"min_score={current_min}"
                   + (f", max_score={current_max}" if current_max else "") + ".")

        # Paper trade outcome breakdown
        cur.execute("""
            SELECT status, COUNT(*), ROUND(AVG(pnl_pct), 2),
                   ROUND(AVG(time_in_trade_hours), 1)
            FROM paper_trades WHERE status != 'open'
            GROUP BY status
        """)
        trade_rows = [{"status": r[0], "n": r[1], "avg_pnl": r[2],
                       "avg_hours": r[3]} for r in cur.fetchall()]

        # Current BTC regime
        try:
            regime = "bull" if ai_trader.btc_regime_bullish() else "bear"
        except Exception:
            regime = "unknown"

        conn.close()

        return jsonify({
            "current_min_score": current_min,
            "current_max_score": current_max,
            "btc_regime":        regime,
            "recommendation":    msg,
            "score_buckets":     buckets,
            "paper_trades":      trade_rows,
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


def _mask_secret(s: str) -> str:
    """Show 'configured (****abcd)' or '***unset/default***' — never the full value."""
    if not s or s == "CHANGE_ME":
        return "*** UNSET — set WEBHOOK_AUTH_TOKEN before exposing publicly ***"
    return f"configured (****{s[-4:]})" if len(s) >= 4 else "configured (short)"


# Start scheduler at module load — works for both `python webhook_server.py`
# (dev) and `gunicorn webhook_server:app` (production). Gunicorn never reaches
# the __main__ block, so this is the only reliable startup hook.
port = int(os.environ.get("PORT", 8080))
print(f"Webhook server starting on :{port}")
print(f"Auth token: {_mask_secret(AUTH_TOKEN)}")
print(f"Anthropic key: {'configured' if ANTHROPIC_KEY else 'not set'}")
print(f"Decider: {'Claude LLM' if USE_LLM and ANTHROPIC_KEY else 'rules-based'}")
start_scheduler()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=port, debug=False)
