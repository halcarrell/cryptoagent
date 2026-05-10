#!/usr/bin/env python3
"""
Discord notifier for paper trade events.

Set DISCORD_WEBHOOK_URL as an environment variable.
If the variable is not set, all calls are silent no-ops.

Usage (called automatically from ai_trader.open_paper_trade):
    from notifier import notify_trade_opened, notify_trade_closed
"""

import os
import json
import requests
from datetime import datetime, timezone

WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL", "")


def _post(payload: dict) -> None:
    if not WEBHOOK_URL:
        return
    try:
        requests.post(WEBHOOK_URL, json=payload, timeout=5)
    except Exception as e:
        print(f"[notifier] Discord post failed: {e}", flush=True)


def notify_trade_opened(trade: dict) -> None:
    """Fire when a paper trade opens. trade = dict from paper_trades row."""
    sym    = trade.get("symbol", "?")
    side   = (trade.get("side") or "long").upper()
    entry  = trade.get("entry_price", 0)
    stop   = trade.get("stop_price", 0)
    target = trade.get("target_price", 0)
    size   = trade.get("size_pct", 0)
    conf   = trade.get("confidence", 0)
    reason = trade.get("reasoning", "")
    tid    = trade.get("trade_id", "?")

    risk   = abs(entry - stop)
    reward = abs(target - entry)
    rr     = f"{reward/risk:.2f}" if risk else "—"

    color  = 0x2ECC71  # green

    _post({
        "embeds": [{
            "title": f"📈 Paper trade opened #{tid}",
            "color": color,
            "fields": [
                {"name": "Symbol",     "value": sym,              "inline": True},
                {"name": "Side",       "value": side,             "inline": True},
                {"name": "R:R",        "value": rr,               "inline": True},
                {"name": "Entry",      "value": f"${entry:,.4g}", "inline": True},
                {"name": "Stop",       "value": f"${stop:,.4g}",  "inline": True},
                {"name": "Target",     "value": f"${target:,.4g}","inline": True},
                {"name": "Size",       "value": f"{size:.1f}%",   "inline": True},
                {"name": "Confidence", "value": f"{conf:.0%}",    "inline": True},
            ],
            "description": reason[:200] if reason else None,
            "footer": {"text": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")},
        }]
    })


def notify_trade_closed(trade: dict) -> None:
    """Fire when a paper trade closes. trade = dict from paper_trades row."""
    sym    = trade.get("symbol", "?")
    status = trade.get("status", "closed").upper()
    pnl    = trade.get("pnl_pct", 0) or 0
    tid    = trade.get("trade_id", "?")

    color  = 0x2ECC71 if pnl >= 0 else 0xE74C3C  # green / red
    emoji  = "✅" if status == "TARGET" else "🛑"

    _post({
        "embeds": [{
            "title": f"{emoji} Paper trade closed #{tid} — {status}",
            "color": color,
            "fields": [
                {"name": "Symbol", "value": sym,              "inline": True},
                {"name": "P&L",    "value": f"{pnl:+.2f}%",  "inline": True},
            ],
            "footer": {"text": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")},
        }]
    })
