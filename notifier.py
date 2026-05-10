#!/usr/bin/env python3
"""
Notifications for the crypto screener agent.

Channels:
  - Discord  : via DISCORD_WEBHOOK_URL env var (no bot needed)
  - Email    : via Gmail SMTP using EMAIL_FROM / EMAIL_TO / EMAIL_APP_PASSWORD

All calls are silent no-ops when env vars are not set, so local runs
are never interrupted by missing config.

Environment variables:
  DISCORD_WEBHOOK_URL   — Discord channel webhook URL
  EMAIL_FROM            — Gmail address to send from (e.g. you@gmail.com)
  EMAIL_TO              — Recipient address (can be the same as EMAIL_FROM)
  EMAIL_APP_PASSWORD    — Gmail App Password (not your regular password)
                          Generate at: myaccount.google.com/apppasswords
"""

import os
import json
import smtplib
import traceback
from datetime import datetime, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import requests

DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL", "")
EMAIL_FROM          = os.environ.get("EMAIL_FROM", "")
EMAIL_TO            = os.environ.get("EMAIL_TO", "")
EMAIL_APP_PASSWORD  = os.environ.get("EMAIL_APP_PASSWORD", "")


# ─────────────────────────────── Discord ────────────────────────────────────

def _discord_post(payload: dict) -> None:
    if not DISCORD_WEBHOOK_URL:
        return
    try:
        r = requests.post(DISCORD_WEBHOOK_URL, json=payload, timeout=5)
        if r.status_code not in (200, 204):
            print(f"[notifier] Discord returned {r.status_code}: {r.text[:200]}", flush=True)
    except Exception as e:
        print(f"[notifier] Discord post failed: {e}", flush=True)


def notify_daily_picks(picks: list, date: str, warnings: list = None) -> None:
    """Post today's top picks to Discord.

    picks: list of dicts with keys symbol, composite_score, rank
    date: YYYY-MM-DD
    warnings: optional list of warning strings
    """
    if not picks:
        return

    rows = "\n".join(
        f"#{p['rank']:<3} {p['symbol']:<8} score={p['composite_score']:+.2f}"
        for p in picks[:10]
    )
    description = f"```\n{rows}\n```"
    if warnings:
        description += "\n⚠ " + "\n⚠ ".join(warnings)

    _discord_post({
        "embeds": [{
            "title": f"📊 Daily picks — {date}",
            "color": 0x3498DB,
            "description": description,
            "footer": {
                "text": f"Screener ran at {datetime.now(timezone.utc).strftime('%H:%M UTC')} • "
                        f"Set alerts in TradingView for these symbols"
            },
        }]
    })


def notify_cron_failure(step: str, error: str) -> None:
    """Red Discord alert when the daily cron fails."""
    _discord_post({
        "embeds": [{
            "title": "🔴 Cron failure — action needed",
            "color": 0xE74C3C,
            "fields": [
                {"name": "Step failed", "value": step,        "inline": False},
                {"name": "Error",       "value": error[:500], "inline": False},
            ],
            "footer": {"text": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")},
        }]
    })


def notify_trade_opened(trade: dict) -> None:
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

    _discord_post({
        "embeds": [{
            "title": f"📈 Paper trade opened #{tid}",
            "color": 0x2ECC71,
            "fields": [
                {"name": "Symbol",     "value": sym,               "inline": True},
                {"name": "Side",       "value": side,              "inline": True},
                {"name": "R:R",        "value": rr,                "inline": True},
                {"name": "Entry",      "value": f"${entry:,.4g}",  "inline": True},
                {"name": "Stop",       "value": f"${stop:,.4g}",   "inline": True},
                {"name": "Target",     "value": f"${target:,.4g}", "inline": True},
                {"name": "Size",       "value": f"{size:.1f}%",    "inline": True},
                {"name": "Confidence", "value": f"{conf:.0%}",     "inline": True},
            ],
            "description": reason[:200] if reason else None,
            "footer": {"text": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")},
        }]
    })


def notify_trade_closed(trade: dict) -> None:
    sym    = trade.get("symbol", "?")
    status = trade.get("status", "closed").upper()
    pnl    = trade.get("pnl_pct", 0) or 0
    tid    = trade.get("trade_id", "?")

    color = 0x2ECC71 if pnl >= 0 else 0xE74C3C
    emoji = "✅" if status == "TARGET" else "🛑"

    _discord_post({
        "embeds": [{
            "title": f"{emoji} Paper trade closed #{tid} — {status}",
            "color": color,
            "fields": [
                {"name": "Symbol", "value": sym,             "inline": True},
                {"name": "P&L",    "value": f"{pnl:+.2f}%", "inline": True},
            ],
            "footer": {"text": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")},
        }]
    })


# ──────────────────────────────── Email ─────────────────────────────────────

def _send_email(subject: str, html_body: str, text_body: str) -> None:
    """Send via Gmail SMTP. Silent no-op if env vars are not set."""
    if not all([EMAIL_FROM, EMAIL_TO, EMAIL_APP_PASSWORD]):
        return
    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"]    = EMAIL_FROM
        msg["To"]      = EMAIL_TO
        msg.attach(MIMEText(text_body, "plain"))
        msg.attach(MIMEText(html_body, "html"))

        with smtplib.SMTP("smtp.gmail.com", 587) as server:
            server.ehlo()
            server.starttls()
            server.login(EMAIL_FROM, EMAIL_APP_PASSWORD)
            server.sendmail(EMAIL_FROM, EMAIL_TO, msg.as_string())
        print("[notifier] Health email sent.", flush=True)
    except Exception as e:
        print(f"[notifier] Email failed: {e}", flush=True)


def send_health_report(
    date: str,
    picks: list,
    paper_stats: dict,
    coverage: dict,
    warnings: list = None,
    refit_note: str = None,
) -> None:
    """
    Send the daily health check email.

    picks        : list of pick dicts (symbol, rank, composite_score, entry_price)
    paper_stats  : {"open": int, "closed_today": int, "hit_rate": float,
                    "avg_pnl": float, "total_closed": int}
    coverage     : {"1d": float, "3d": float, "7d": float}  (0-100 %)
    warnings     : list of warning strings
    refit_note   : optional string from weight_refitter
    """
    warnings = warnings or []
    now_str  = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    # ── plain text ──────────────────────────────────────────────────
    lines = [
        f"Crypto Screener Health Report — {date}",
        f"Generated: {now_str}",
        "",
        "═══ TODAY'S PICKS ═══",
    ]
    for p in picks[:10]:
        lines.append(f"  #{p['rank']:<2} {p['symbol']:<8} score={p['composite_score']:+.2f}  "
                     f"@ ${p['entry_price']:.4g}")
    lines += [
        "",
        "═══ PAPER TRADING ═══",
        f"  Open positions  : {paper_stats.get('open', 0)}",
        f"  Total closed    : {paper_stats.get('total_closed', 0)}",
        f"  Hit rate        : {paper_stats.get('hit_rate', 0):.1f}%",
        f"  Avg P&L         : {paper_stats.get('avg_pnl', 0):+.2f}%",
        "",
        "═══ DATA COVERAGE ═══",
        f"  1d realized: {coverage.get('1d', 0):.1f}%",
        f"  3d realized: {coverage.get('3d', 0):.1f}%",
        f"  7d realized: {coverage.get('7d', 0):.1f}%",
    ]
    if warnings:
        lines += ["", "═══ WARNINGS ═══"] + [f"  ⚠ {w}" for w in warnings]
    if refit_note:
        lines += ["", "═══ WEIGHT REFIT ═══", f"  {refit_note}"]
    text_body = "\n".join(lines)

    # ── HTML ────────────────────────────────────────────────────────
    pick_rows = "".join(
        f"<tr><td>#{p['rank']}</td><td><b>{p['symbol']}</b></td>"
        f"<td>{p['composite_score']:+.2f}</td>"
        f"<td>${p['entry_price']:.4g}</td></tr>"
        for p in picks[:10]
    )
    warn_html = (
        "<p style='color:#e74c3c'><b>Warnings</b><br>" +
        "<br>".join(f"⚠ {w}" for w in warnings) + "</p>"
        if warnings else ""
    )
    refit_html = (
        f"<p><b>Weight refit:</b> {refit_note}</p>" if refit_note else ""
    )
    hit_rate = paper_stats.get("hit_rate", 0)
    hit_color = "#2ecc71" if hit_rate >= 50 else "#e74c3c"

    html_body = f"""
    <html><body style="font-family:sans-serif;max-width:600px;margin:auto">
    <h2 style="color:#2c3e50">📊 Crypto Screener — {date}</h2>
    <p style="color:#7f8c8d;font-size:12px">{now_str}</p>

    <h3>Today's Top Picks</h3>
    <table border="0" cellpadding="6" style="border-collapse:collapse;width:100%">
      <tr style="background:#ecf0f1;font-weight:bold">
        <td>#</td><td>Symbol</td><td>Score</td><td>Price</td>
      </tr>
      {pick_rows}
    </table>

    <h3>Paper Trading</h3>
    <table border="0" cellpadding="6" style="border-collapse:collapse">
      <tr><td>Open positions</td><td><b>{paper_stats.get('open', 0)}</b></td></tr>
      <tr><td>Total closed</td><td><b>{paper_stats.get('total_closed', 0)}</b></td></tr>
      <tr><td>Hit rate</td>
          <td><b style="color:{hit_color}">{hit_rate:.1f}%</b></td></tr>
      <tr><td>Avg P&L</td>
          <td><b>{paper_stats.get('avg_pnl', 0):+.2f}%</b></td></tr>
    </table>

    <h3>Data Coverage</h3>
    <p>1d: {coverage.get('1d', 0):.1f}% &nbsp;|&nbsp;
       3d: {coverage.get('3d', 0):.1f}% &nbsp;|&nbsp;
       7d: {coverage.get('7d', 0):.1f}%</p>

    {warn_html}
    {refit_html}

    <hr>
    <p style="font-size:11px;color:#95a5a6">
      This is an automated report from your crypto screener agent.<br>
      Paper trading only — no real money is at risk.
    </p>
    </body></html>
    """

    subject = f"Crypto Screener — {date}"
    if warnings:
        subject += f" ⚠ {len(warnings)} warning(s)"

    _send_email(subject, html_body, text_body)
