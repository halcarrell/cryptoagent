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

from flask import Flask, jsonify, request

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


def _mask_secret(s: str) -> str:
    """Show 'configured (****abcd)' or '***unset/default***' — never the full value."""
    if not s or s == "CHANGE_ME":
        return "*** UNSET — set WEBHOOK_AUTH_TOKEN before exposing publicly ***"
    return f"configured (****{s[-4:]})" if len(s) >= 4 else "configured (short)"


if __name__ == "__main__":
    # DB already initialized at module level (with error handling).
    # Railway injects PORT; falls back to 8080 for local dev.
    port = int(os.environ.get("PORT", 8080))
    print(f"Webhook server starting on :{port}")
    # Never log the auth token in plaintext — masked so logs/screenshots don't leak it.
    print(f"Auth token: {_mask_secret(AUTH_TOKEN)}")
    print(f"Anthropic key: {'configured' if ANTHROPIC_KEY else 'not set'}")
    print(f"Decider: {'Claude LLM' if USE_LLM and ANTHROPIC_KEY else 'rules-based'}")
    app.run(host="0.0.0.0", port=port, debug=False)
