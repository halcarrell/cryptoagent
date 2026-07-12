import os
import sys
from datetime import datetime, timezone
import requests

BASE = "http://charming-possibility.railway.internal:8080"
TOKEN = os.environ["WEBHOOK_AUTH_TOKEN"]


def run():
    try:
        r = requests.get(f"{BASE}/status", params={"auth": TOKEN}, timeout=10)
        r.raise_for_status()
        data = r.json()
        alerts      = data.get("recent_alerts_72h", 0)
        picks_fresh = data.get("picks_fresh", False)
        open_trades = data.get("open_trades", 0)
        print(f"Status OK: alerts={alerts}, picks_fresh={picks_fresh}, open={open_trades}")
    except Exception as e:
        print(f"Status check failed: {e}", file=sys.stderr)
        alerts      = -1
        picks_fresh = False
        open_trades = 0

    # Screener runs at 13:00 UTC. picks_fresh is False before then — not a problem.
    # Flag a concern only when BOTH today AND yesterday have no picks, meaning the
    # screener has missed at least one full day (not just the pre-13:00 window).
    now_utc = datetime.now(timezone.utc)
    picks_yesterday = data.get("picks_yesterday", 0)
    picks_concern = not picks_fresh and picks_yesterday == 0

    if alerts == -1:
        title = "⚠️ Monitor Error"
        desc  = "Could not reach the webhook server to check alert status."
        color = 15158332
    elif alerts == 0:
        title = "⚠️ TradingView Alert Gap"
        desc  = "No TradingView webhooks received in the last 72 hours."
        color = 16776960
    else:
        title = "✅ Alert health OK"
        picks_line = ""
        if picks_concern:
            picks_line = "\n⚠️ Picks not refreshed — screener may have failed (past 14:00 UTC)."
        elif not picks_fresh:
            picks_line = "\nPickup pending — screener runs at 13:00 UTC."
        desc = (f"{alerts} TradingView webhooks in 72h · {open_trades} open trade(s)"
                f"{picks_line}")
        color = 15158332 if picks_concern else 3066993

    try:
        resp = requests.post(
            f"{BASE}/notify",
            json={"auth": TOKEN, "embeds": [{"title": title, "color": color, "description": desc}]},
            timeout=10,
        )
        print(f"Notify response: {resp.status_code} {resp.text}")
    except Exception as e:
        print(f"Notify failed: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    run()
