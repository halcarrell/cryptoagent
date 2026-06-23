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
        print("[notifier] DISCORD_WEBHOOK_URL not set — message not sent", flush=True)
        return
    try:
        r = requests.post(DISCORD_WEBHOOK_URL, json=payload, timeout=5)
        if r.status_code not in (200, 204):
            print(f"[notifier] Discord error {r.status_code}: {r.text[:200]}", flush=True)
        else:
            title = ""
            try:
                title = payload["embeds"][0].get("title", "")[:60]
            except Exception:
                pass
            print(f"[notifier] Discord sent: {title}", flush=True)
    except Exception as e:
        print(f"[notifier] Discord post failed: {e}", flush=True)


def notify_daily_picks(picks: list, date: str, warnings: list = None,
                       watchlist_symbols: list = None,
                       news_by_symbol: dict = None,
                       paper_stats: dict = None,
                       short_watch: list = None) -> None:
    """Post today's top picks to Discord, including TradingView symbols to set alerts on."""
    if not picks:
        return

    rows = "\n".join(
        f"#{p['rank']:<3} {p['symbol']:<8} score={p['composite_score']:+.2f}"
        for p in picks[:10]
    )
    description = f"```\n{rows}\n```"

    if watchlist_symbols:
        tv_list = "\n".join(watchlist_symbols)
        description += f"\n**Set alerts on these in TradingView:**\n```\n{tv_list}\n```"

    if warnings:
        description += "\n⚠ " + "\n⚠ ".join(warnings)

    fields = []

    if paper_stats and (paper_stats.get("total_closed") or paper_stats.get("open")):
        open_n   = paper_stats.get("open", 0)
        closed_n = paper_stats.get("total_closed", 0)
        hit      = paper_stats.get("hit_rate", 0.0)
        avg_pnl  = paper_stats.get("avg_pnl", 0.0)
        pnl_7d   = paper_stats.get("rolling_7d_pnl", None)
        pnl_emoji = "📈" if (avg_pnl or 0) >= 0 else "📉"
        stats_lines = [
            f"Open: **{open_n}** • Closed: **{closed_n}**",
            f"Hit rate: **{hit:.1f}%** • Avg P&L: **{avg_pnl:+.2f}%** {pnl_emoji}",
        ]
        if pnl_7d is not None:
            w7_emoji = "📈" if pnl_7d >= 0 else "📉"
            stats_lines.append(f"7d weighted P&L: **{pnl_7d:+.2f}%** {w7_emoji}")
        fields.append({
            "name": "📋 Paper Trading",
            "value": "\n".join(stats_lines),
            "inline": False,
        })

    if short_watch:
        short_lines = " · ".join(
            f"**{s['symbol']}** `{s['score']:+.2f}`" for s in short_watch[:5]
        )
        fields.append({
            "name": "📉 Short Watch (weakest scores today)",
            "value": short_lines + "\n*Server opens short on Pine signal + bear regime + score cap*",
            "inline": False,
        })

    if news_by_symbol:
        try:
            from news_fetcher import format_news_lines
            headline_lines = []
            for p in picks[:10]:
                sym = p["symbol"].upper()
                coin_news = news_by_symbol.get(sym, [])
                if coin_news:
                    lines = format_news_lines(coin_news[:1])
                    headline_lines.append(f"**{sym}** — {lines[0]}")
            if headline_lines:
                fields.append({
                    "name": "📰 Headlines",
                    "value": "\n".join(headline_lines[:10]),
                    "inline": False,
                })
        except Exception:
            pass

    embed = {
        "title": f"📊 Daily picks — {date}",
        "color": 0x3498DB,
        "description": description,
        "footer": {
            "text": f"Screener ran at {datetime.now(timezone.utc).strftime('%H:%M UTC')} • "
                    f"Watchlist file also written to /data/watchlist_{date}_binance.txt"
        },
    }
    if fields:
        embed["fields"] = fields

    _discord_post({"embeds": [embed]})


def notify_strong_signals(signals: list, date: str) -> None:
    """Post a ⚡ Strong Signals alert when exceptional conditions are detected."""
    if not signals:
        return
    fields = [
        {
            "name": f"{s['emoji']}  {s['symbol']}  —  {s['signal_type'].replace('_',' ').title()}",
            "value": s["detail"],
            "inline": False,
        }
        for s in signals[:10]
    ]
    _discord_post({
        "embeds": [{
            "title": f"⚡ Strong Signals — {date}",
            "color": 0xF39C12,
            "description": "These picks show exceptional characteristics. Worth watching closely today.",
            "fields": fields,
            "footer": {"text": "Strong signal ≠ guaranteed trade — still requires Pine confirmation"},
        }]
    })


def notify_pine_snippet(pine_block: str, date: str) -> None:
    """Post the daily get_score() Pine Script block to Discord.
    User copies this and pastes into Pine Editor to auto-update all chart scores."""
    if not pine_block:
        return
    _discord_post({
        "embeds": [{
            "title": f"🌲 Pine Script scores — {date}",
            "color": 0x1ABC9C,
            "description": (
                "Copy the block below and paste it into your indicator in Pine Editor "
                "(between the dashed lines), then click **Save**. "
                "All charts update automatically.\n\n"
                f"```pine\n{pine_block}\n```"
            ),
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


def notify_risk_rejection(symbol: str, reason: str) -> None:
    """Orange Discord alert when the risk monitor blocks a paper trade."""
    _discord_post({
        "embeds": [{
            "title": f"🛡 Risk block — {symbol}",
            "color": 0xE67E22,
            "description": (
                f"A trade entry was blocked by the portfolio risk monitor.\n\n"
                f"**Reason:** {reason}"
            ),
            "footer": {"text": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")},
        }]
    })


def notify_signal_passed(symbol: str, reason: str, side: str = "long",
                         regime: str = "") -> None:
    """Grey Discord card when a signal fires but the server skips the trade.

    Sent at most once per 4 hours (cooldown enforced in webhook_server.py)
    so repeated signals in the same bear stretch don't flood the channel.
    """
    side_emoji = "🟢" if (side or "long") == "long" else "🔴"
    regime_line = ""
    if regime:
        icons = {"bull": "📈 BULL", "sideways": "↔️ SIDEWAYS", "bear": "📉 BEAR"}
        regime_line = f"\n**BTC Regime:** {icons.get(regime, regime.upper())}"
    _discord_post({
        "embeds": [{
            "title": f"⏸ Signal skipped — {symbol}",
            "color": 0x95A5A6,
            "description": (
                f"{side_emoji} **{(side or 'long').upper()}** signal received but not traded."
                f"{regime_line}\n\n"
                f"**Reason:** {reason}\n\n"
                f"*System trades decorrelated coins in sideways/bear markets — "
                f"check daily picks for decorrelation scores.*"
            ),
            "footer": {"text": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")},
        }]
    })


def notify_signal_received(alert: dict, decision, source: str = "tradingview") -> None:
    """Human-readable Discord card posted for EVERY signal — traded or not.

    This is the card followers actually act on. It shows the full signal
    (direction, prices, R:R) plus what the server decided and why.
    Age label makes it clear whether the signal is still actionable.
    """
    sym    = alert.get("symbol", "?")
    side   = (alert.get("side") or "long").upper()
    entry  = alert.get("entry") or 0
    stop   = alert.get("stop")  or 0
    target = alert.get("target") or 0
    rsi    = alert.get("rsi")
    age_s  = alert.get("signal_age_secs", 0) or 0

    risk   = abs(entry - stop)
    reward = abs(target - entry)
    rr     = reward / risk if risk else 0
    stop_pct   = (stop   - entry) / entry * 100 if entry else 0
    target_pct = (target - entry) / entry * 100 if entry else 0

    def fmt(p): return f"${p:,.6g}"

    side_emoji  = "🟢" if side == "LONG" else "🔴"
    source_tag  = "📡 SCANNER" if source == "scanner" else "📺 TRADINGVIEW"

    # Age label
    if age_s < 120:
        age_label = "🟢 fresh"
        age_color_warn = ""
    elif age_s < 900:
        age_label = f"🟡 {age_s//60}m delayed"
        age_color_warn = f" *(signal is {age_s//60} min old — verify price before acting)*\n"
    elif age_s < 3600:
        age_label = f"🟠 {age_s//60}m delayed"
        age_color_warn = f" *(signal is {age_s//60} min old — price may have moved)*\n"
    else:
        age_label = f"🔴 {age_s//3600}h old"
        age_color_warn = f" *(signal is {age_s//3600}h old — informational only)*\n"

    # Server verdict line
    action = decision.action if decision else "unknown"
    reason = (decision.reasoning or "") if decision else ""
    regime = (decision.regime or "") if decision else ""

    if action == "enter":
        size_pct = decision.size_pct if decision else 0
        conv     = (decision.conviction or "").upper() if decision else ""
        verdict  = f"✅ **PAPER TRADE OPENED** — {size_pct:.1f}% position · {conv} conviction"
        color    = 0x2ECC71 if side == "LONG" else 0xE74C3C
    else:
        verdict  = f"⏸ **NOT TRADED** — {reason[:120]}"
        color    = 0x95A5A6

    regime_icon = {"RISK_ON": "📈", "RISK_OFF": "📉", "CHOP": "↔️"}.get(regime, "")
    rsi_str = f" · RSI {rsi:.0f}" if rsi is not None else ""

    url  = _binance_us_url(sym)
    now  = datetime.now(timezone.utc).strftime("%H:%M UTC")

    lines = [
        age_color_warn,
        f"Entry **{fmt(entry)}** · Stop **{fmt(stop)}** `{stop_pct:+.1f}%` · Target **{fmt(target)}** `{target_pct:+.1f}%`",
        f"R:R **{rr:.1f}:1**{rsi_str} · {regime_icon} {regime}",
        "",
        verdict,
    ]
    if action == "enter":
        lines.append(f"\n[🚀 Execute on Binance.US →]({url})")

    _discord_post({
        "embeds": [{
            "title": f"{side_emoji} {side} SIGNAL — {sym}  [{source_tag} · {age_label}]",
            "color": color,
            "description": "\n".join(lines),
            "footer": {"text": f"Signal at {now}"},
        }]
    })


def _binance_us_url(symbol: str) -> str:
    """Build a direct Binance.US trading page link for a symbol like SOLUSDT."""
    base = symbol.upper().replace("USDT", "").replace("USD", "")
    return f"https://www.binance.us/trade/pro/{base}_USDT"


def notify_trade_opened(trade: dict, news: list = None) -> None:
    sym        = trade.get("symbol", "?")
    side       = (trade.get("side") or "long").upper()
    entry      = trade.get("entry_price") or 0
    stop       = trade.get("stop_price")  or 0
    target     = trade.get("target_price") or 0
    t1         = trade.get("target_1_price")
    size       = trade.get("size_pct") or 0
    conf       = trade.get("confidence") or 0
    tid        = trade.get("trade_id", "?")
    signal_id  = trade.get("signal_id", "")
    regime     = trade.get("regime", "")
    conviction = (trade.get("conviction") or "low").upper()
    thesis     = trade.get("thesis", "")
    inv        = trade.get("invalidation", "")
    cat_str    = trade.get("catalyst_strength", "none")
    cat_note   = trade.get("catalyst_note", "")
    rs         = trade.get("relative_strength", "inline")

    risk   = abs(entry - stop)
    reward = abs(target - entry)
    rr     = reward / risk if risk else 0
    stop_pct   = (stop   - entry) / entry * 100 if entry else 0
    target_pct = (target - entry) / entry * 100 if entry else 0

    dollar_size = PORTFOLIO_USD * size / 100
    quantity    = dollar_size / entry if entry else 0

    def fmt(p): return f"${p:,.6g}"

    action_emoji = "🟢" if side == "LONG" else "🔴"
    action_word  = "BUY" if side == "LONG" else "SELL SHORT"
    url  = _binance_us_url(sym)
    now  = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    # Conviction badge
    conv_emoji = {"HIGH": "🔥", "MED": "⚡", "LOW": "💧"}.get(conviction, "💧")
    regime_icon = {"RISK_ON": "📈", "RISK_OFF": "📉", "CHOP": "↔️"}.get(regime, "")
    rs_icon = {"leader": "🚀", "laggard": "🐌"}.get(rs, "➡️")

    description = (
        f"{conv_emoji} **{conviction} conviction** · {regime_icon} {regime} · {rs_icon} {rs}\n\n"
        f"*{thesis}*\n\n"
        f"Entry **{fmt(entry)}** · R:R **{rr:.1f}:1** · Size **${dollar_size:,.0f}**\n"
        f"Stop **{fmt(stop)}** `{stop_pct:+.1f}%` → "
    )
    if t1:
        t1_pct = (t1 - entry) / entry * 100 if side == "LONG" else (entry - t1) / entry * 100
        description += f"T1 **{fmt(t1)}** `{t1_pct:+.1f}%` → "
    description += (
        f"Target **{fmt(target)}** `{target_pct:+.1f}%`\n"
        f"Conf **{conf:.0%}** · ~{quantity:,.4g} units\n\n"
        f"## [🚀 Execute on Binance.US →]({url})\n"
        f"*{action_word} {sym} at market · SL {fmt(stop)} · TP {fmt(target)}*"
    )
    if inv:
        description += f"\n⚠️ **Exit if:** {inv}"

    fields = []
    if cat_str != "none" and cat_note:
        cat_emoji = "💥" if cat_str == "strong" else "📌"
        fields.append({
            "name": f"{cat_emoji} Catalyst ({cat_str})",
            "value": cat_note[:200],
            "inline": False,
        })
    if news:
        try:
            from news_fetcher import format_news_lines
            lines = format_news_lines(news[:3])
            if lines:
                fields.append({
                    "name": "📰 Recent News",
                    "value": "\n".join(lines),
                    "inline": False,
                })
        except Exception:
            pass

    footer_parts = [f"Portfolio ${PORTFOLIO_USD:,.0f}", f"Size {size:.1f}%", now]
    if signal_id:
        footer_parts.append(f"ID: {signal_id}")

    embed = {
        "title":       f"{action_emoji} PAPER {action_word} — {sym}  (trade #{tid})",
        "color":       0x2ECC71 if side == "LONG" else 0xE74C3C,
        "description": description,
        "footer":      {"text": "  •  ".join(footer_parts)},
    }
    if fields:
        embed["fields"] = fields

    _discord_post({"embeds": [embed]})


def notify_trade_closed(trade: dict) -> None:
    sym       = trade.get("symbol", "?")
    status    = trade.get("status", "closed").upper()
    pnl       = trade.get("pnl_pct", 0) or 0
    entry     = trade.get("entry_price") or 0
    exit_p    = trade.get("exit_price")  or 0
    size      = trade.get("size_pct") or 0
    tid       = trade.get("trade_id", "?")
    signal_id = trade.get("signal_id", "")
    thesis    = trade.get("thesis", "")
    tag       = trade.get("outcome_tag", "")
    t1_done   = trade.get("tranche1_already_closed", False)
    now       = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    # Tranche1 partial close has its own presentation
    is_tranche1 = (status == "TRANCHE1")
    actual_size = size * 0.5 if is_tranche1 else size
    dollar_pnl  = PORTFOLIO_USD * actual_size / 100 * pnl / 100

    if is_tranche1:
        emoji, color = "🎯", 0x27AE60
        title   = f"🎯 PARTIAL EXIT (50%) — {sym}  (#{tid})"
        outcome = "First target hit — 50% closed, stop moved to breakeven, trailing remainder"
    elif status == "TARGET":
        emoji, color = "✅", 0x2ECC71
        title   = f"✅ TARGET HIT — {sym}  (#{tid})"
        outcome = "Full target reached" + (" — second tranche" if t1_done else "")
    else:
        emoji, color = "🛑", 0xE74C3C
        is_trail = t1_done and status == "STOPPED"
        title    = f"🛑 {'TRAILING STOP' if is_trail else 'STOP HIT'} — {sym}  (#{tid})"
        outcome  = ("Trailing stop triggered — locked in partial gains" if is_trail
                    else "Stop hit — loss taken")

    tag_line = f"  ·  {tag}" if tag and tag not in ("UNKNOWN",) else ""
    description = (
        f"**{outcome}**\n\n"
        f"Entry `{entry:,.6g}` → Exit `{exit_p:,.6g}`\n"
        f"P&L **{pnl:+.2f}%** ≈ **${dollar_pnl:+,.0f}**{tag_line}"
    )
    if thesis:
        description += f"\n*Original thesis: {thesis[:120]}*"

    # v2 §8: always emit action=CLOSE so followers know to exit
    close_action = f"action=CLOSE · signal_id={signal_id}" if signal_id else "action=CLOSE"

    footer_parts = [now, close_action]

    _discord_post({
        "embeds": [{
            "title":       title,
            "color":       color,
            "description": description,
            "footer":      {"text": "  •  ".join(footer_parts)},
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
    except OSError as e:
        if getattr(e, "errno", None) == 101 or "unreachable" in str(e).lower():
            print("[notifier] Email skipped — Railway blocks outbound SMTP (port 587). "
                  "Email health reports are disabled on Railway; use Discord instead.",
                  flush=True)
        else:
            print(f"[notifier] Email failed: {e}", flush=True)
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
