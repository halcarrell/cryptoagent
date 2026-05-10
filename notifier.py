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
PORTFOLIO_USD       = float(os.environ.get("PORTFOLIO_USD", "10000"))
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


def _binance_us_url(symbol: str) -> str:
    """Build a direct Binance.US trading page link for a symbol like SOLUSDT."""
    base = symbol.upper().replace("USDT", "").replace("USD", "")
    return f"https://www.binance.us/trade/pro/{base}_USDT"


def notify_trade_opened(trade: dict) -> None:
    sym    = trade.get("symbol", "?")
    side   = (trade.get("side") or "long").upper()
    entry  = trade.get("entry_price") or 0
    stop   = trade.get("stop_price")  or 0
    target = trade.get("target_price") or 0
    size   = trade.get("size_pct") or 0
    conf   = trade.get("confidence") or 0
    reason = trade.get("reasoning", "")
    tid    = trade.get("trade_id", "?")

    # Price maths
    risk        = abs(entry - stop)
    reward      = abs(target - entry)
    rr          = reward / risk if risk else 0
    stop_pct    = (stop   - entry) / entry * 100 if entry else 0
    target_pct  = (target - entry) / entry * 100 if entry else 0

    # Dollar + quantity sizing
    dollar_size = PORTFOLIO_USD * size / 100
    quantity    = dollar_size / entry if entry else 0

    # Format prices — more decimals for sub-$1 coins
    def fmt(p): return f"${p:,.6g}"

    action_emoji = "🟢" if side == "LONG" else "🔴"
    action_word  = "BUY" if side == "LONG" else "SELL"
    url          = _binance_us_url(sym)
    now          = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    description = (
        f"**{fmt(entry)}** entry\n"
        f"Stop loss  **{fmt(stop)}**  `{stop_pct:+.1f}%`\n"
        f"Take profit  **{fmt(target)}**  `{target_pct:+.1f}%`\n"
        f"R:R  **{rr:.1f}:1**  •  Confidence  **{conf:.0%}**\n\n"
        f"**What to do:**\n"
        f"1️⃣  [Open {sym} on Binance.US]({url})\n"
        f"2️⃣  {action_word}  **${dollar_size:,.0f}**  at market  (~{quantity:,.4g} units)\n"
        f"3️⃣  Set stop-loss at  **{fmt(stop)}**\n"
        f"4️⃣  Set take-profit at  **{fmt(target)}**\n\n"
        f"*{reason[:180]}*" if reason else ""
    )

    _discord_post({
        "embeds": [{
            "title":       f"{action_emoji} PAPER {action_word} — {sym}  (trade #{tid})",
            "color":       0x2ECC71 if side == "LONG" else 0xE74C3C,
            "description": description,
            "footer":      {"text": f"Portfolio ${PORTFOLIO_USD:,.0f}  •  Size {size:.1f}%  •  {now}"},
        }]
    })


def notify_trade_closed(trade: dict) -> None:
    sym    = trade.get("symbol", "?")
    status = trade.get("status", "closed").upper()
    pnl    = trade.get("pnl_pct", 0) or 0
    entry  = trade.get("entry_price") or 0
    exit_p = trade.get("exit_price")  or 0
    size   = trade.get("size_pct") or 0
    tid    = trade.get("trade_id", "?")

    dollar_pnl = PORTFOLIO_USD * size / 100 * pnl / 100
    hit_target = status == "TARGET"
    emoji      = "✅" if hit_target else "🛑"
    color      = 0x2ECC71 if hit_target else 0xE74C3C
    outcome    = "Target hit — take profit" if hit_target else "Stop hit — loss taken"
    now        = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    description = (
        f"**{outcome}**\n\n"
        f"Entry  ${entry:,.6g}  →  Exit  ${exit_p:,.6g}\n"
        f"P&L  **{pnl:+.2f}%**  ≈  **${dollar_pnl:+,.0f}** on this trade"
    )

    _discord_post({
        "embeds": [{
            "title":       f"{emoji} PAPER TRADE CLOSED — {sym}  (#{tid})",
            "color":       color,
            "description": description,
            "footer":      {"text": now},
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
