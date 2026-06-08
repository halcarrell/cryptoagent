import os
import sys
import requests

BASE = "http://charming-possibility.railway.internal:8080"
TOKEN = os.environ["WEBHOOK_AUTH_TOKEN"]


def run():
    try:
        r = requests.get(f"{BASE}/status", params={"auth": TOKEN}, timeout=10)
        r.raise_for_status()
        data = r.json()
        alerts = data.get("recent_alerts_72h", 0)
        picks = data.get("picks_fresh", False)
        print(f"Status OK: alerts={alerts}, picks_fresh={picks}")
    except Exception as e:
        print(f"Status check failed: {e}", file=sys.stderr)
        alerts = -1
        picks = False

    if alerts == -1:
        title = "⚠️ Monitor Error"
        desc = "Could not reach the webhook server to check alert status."
        color = 15158332
    elif alerts == 0:
        title = "⚠️ TradingView Alert Gap"
        desc = "No TradingView webhooks received in the last 72 hours."
        color = 16776960
    else:
        title = "✅ Alert health OK"
        desc = f"{alerts} TradingView webhooks received in the last 72h. Picks fresh: {picks}."
        color = 3066993

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
