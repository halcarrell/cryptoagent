# Crypto Screener Agent — Complete Guide

## What Does This System Do?

Every day your system automatically:
1. **Scans** the top 250 cryptocurrencies and scores them on momentum, volume, trend strength, and other factors
2. **Filters** out pump-and-dump tokens, stablecoins, and coins not available on Binance.US
3. **Picks** the top coins and posts them to Discord with scores
4. **Flags** any exceptional signals (volume surges, near all-time highs, exceptional scores)
5. **Watches** TradingView charts all day for technical confirmations (RSI bounce, VWAP reclaim, volume spike, uptrend)
6. **Receives** alerts from TradingView when a signal fires
7. **Decides** whether to open a paper trade (checks R:R, score, staleness)
8. **Pings Discord** with a trade card when a paper trade opens or closes

> **This is paper trading only.** No real money moves. The goal is 30–60 days of validation before considering live trading.

---

## Accounts You Need

| Service | What For | Cost |
|---|---|---|
| **GitHub** | Stores the code | Free |
| **Railway** | Runs the server 24/7 | ~$5/month |
| **TradingView** | Charts + signal alerts | Essential plan ($15/mo minimum) — webhooks require a paid plan |
| **Discord** | Trade notifications | Free |
| **Binance.US** | Exchange your picks are filtered for | Free to open |

---

## Part 1 — Setup Guide

### Step 1 — Get the Code on GitHub

If the repo isn't already on GitHub:

```bash
cd "/Users/halcarrell/Documents/Claude/Projects/Crypto research/files"
git remote add origin https://github.com/YOUR_USERNAME/cryptoagent.git
git push -u origin main
```

### Step 2 — Deploy to Railway

1. Go to **railway.com** → New Project → Deploy from GitHub → select `cryptoagent`
2. Railway auto-detects Python and starts building
3. Once deployed: click your service → **Settings** → **Networking** → set port to `8080` → **Generate Domain**

**Add environment variables** (Settings → Variables):

| Variable | Value |
|---|---|
| `CRYPTO_AGENT_DB` | `/data/crypto_agent.db` |
| `WEBHOOK_AUTH_TOKEN` | Run `openssl rand -hex 32` in Terminal and paste the result |
| `DISCORD_WEBHOOK_URL` | Your Discord webhook URL (see Step 3) |
| `PORTFOLIO_USD` | Your paper trading budget in dollars (e.g. `10000`) |

**Add a volume** (Settings → Volumes):
- Click **Add Volume** → Mount path: `/data`
- This is where your database lives — it persists through restarts and redeploys

**Verify it's working:**
Open your Railway domain in a browser. You should see:
```json
{"status": "ok", "service": "crypto-screener-webhook", "decider": "rules"}
```
If you see that — your server is live. ✅

### Step 3 — Set Up Discord

1. Open Discord → go to any channel (or create `#crypto-alerts`)
2. Click the gear ⚙ next to the channel → **Integrations** → **Webhooks** → **New Webhook**
3. Name it anything → click **Copy Webhook URL**
4. Paste it into Railway as `DISCORD_WEBHOOK_URL`

> Never share this webhook URL — anyone with it can post to your channel.

### Step 4 — Run the First Screener

Before TradingView alerts can work, you need today's picks in the database:

```bash
cd "/Users/halcarrell/Documents/Claude/Projects/Crypto research/files"
python3 crypto_agent.py fetch
python3 crypto_agent.py report
```

This takes 2–3 minutes. When it finishes, you'll see today's top 10 picks.

### Step 5 — Set Up TradingView

**Add the Pine Script indicator (one-time):**
1. Open TradingView → open any chart
2. Click **Pine Editor** at the bottom
3. Delete any existing code → paste the full contents of `screener_confirmation.pine`
4. Click **Save** → give it a name (e.g. "Screener Confirmation") → click **Add to chart**
5. Click the ⚙ gear next to the indicator name → set **Webhook auth token** to your `WEBHOOK_AUTH_TOKEN` from Railway → click **OK**

> You only need to do this once. The script is static — no daily updates needed.

**Import today's watchlist:**

```bash
python3 tv_integration.py watchlist --exchange BINANCE --filter-exchange BINANCE_US
```

This creates a file like `watchlist_2026-05-11_binance.txt` filtered to coins available on Binance.US.

In TradingView: Watchlist panel → **⋯ three dots** → **Import list** → select the file.

> Always use watchlist import — never type a symbol like "SUI" manually. The import specifies the exact exchange (`BINANCE:SUIUSDT`) so there's no ambiguity.

**Create alerts (one per coin in your watchlist):**
1. Open a chart for each coin (click it in your watchlist)
2. Change timeframe to **4H**
3. Right-click chart → **Add alert**
4. Condition: `Screener Confirmation` → `any alert() function call`
5. Click **Notifications** tab → check **Webhook URL** → paste your Railway domain + `/webhook`:
   ```
   https://YOUR-SERVICE.up.railway.app/webhook
   ```
6. Leave the **Message** field blank — the indicator fills it automatically
7. Click **Create**

Repeat for each coin. Alerts stay active permanently — no daily re-setup needed.

---

## Part 2 — Admin Guide

### What Runs Automatically

| When | What Happens |
|---|---|
| Daily 1pm UTC | Screener fetches 250 coins, scores them, posts top picks to Discord |
| Daily 1pm UTC | ⚡ Strong Signals alert fires if any exceptional conditions detected |
| Daily 1pm UTC | Health check email sent (if email is configured) |
| Sunday 2pm UTC | Weight refitter runs, posts results to Discord |
| Always on | Webhook server receives TradingView alerts 24/7 |

### Is Everything Healthy?

**Check the server:**
Open your Railway domain in a browser — should show `{"status": "ok"}`.

**Check Railway logs:**
Railway dashboard → your service → **Logs** tab → look for:
```
[scheduler] Started — daily@13:00 UTC
DB init OK
Webhook server starting on :8080
```

**Check today's picks:**
```bash
python3 crypto_agent.py report
```

### Setting Up Email Health Reports (Optional)

Add these to your Railway service variables:

| Variable | Value |
|---|---|
| `EMAIL_FROM` | Your Gmail address |
| `EMAIL_TO` | Where to send the report (can be same address) |
| `EMAIL_APP_PASSWORD` | A Gmail App Password — generate at **myaccount.google.com/apppasswords** → Other → name it "crypto screener" |

> Use an App Password, not your regular Gmail password.

### Tuning the Screener

Edit `config.json` to adjust thresholds:

```json
{
  "strong_signals": {
    "score_exceptional": 2.5,    ← lower to see more strong alerts
    "volume_surge_score": 3.0,   ← lower to catch more volume spikes
    "ath_within_pct": 20.0       ← raise to see more ATH candidates
  },
  "risk": {
    "min_risk_reward_gross": 2.0, ← minimum R:R required to trade
    "min_risk_reward_net": 1.6,   ← minimum R:R after 0.1% Binance fees
    "max_position_pct": 5.0,      ← max % of portfolio per trade
    "min_score": 0.5              ← minimum screener score to trade
  }
}
```

After editing, push to GitHub — Railway redeploys automatically.

### Updating the Code

```bash
cd "/Users/halcarrell/Documents/Claude/Projects/Crypto research/files"
git add -A
git commit -m "describe what you changed"
git push
```

Railway rebuilds within a couple of minutes.

### Applying New Screener Weights (After 30+ Days)

Once you have enough paper trade data:

```bash
python3 weight_refitter.py status    # check if enough data
python3 weight_refitter.py validate  # see if new weights improve results
python3 weight_refitter.py refit     # generate new weights.json
```

Review `weights.json` before applying. If the validate output shows consistent improvement, update the `WEIGHTS` dictionary in `crypto_agent.py`.

### Common Problems

**Server returns 502:**
- Check Railway logs for Python errors
- Confirm the volume is attached and `CRYPTO_AGENT_DB=/data/crypto_agent.db` is set
- Redeploy from the Railway dashboard

**TradingView webhook shows 404:**
- URL has a trailing slash — it must end with `/webhook` not `/webhook/`
- Check the exact URL in your alert settings

**TradingView webhook shows 401:**
- The auth token in the indicator settings doesn't match `WEBHOOK_AUTH_TOKEN` in Railway

**Discord not pinging:**
- Confirm `DISCORD_WEBHOOK_URL` is set in Railway variables
- Check Railway logs for `[notifier]` error lines

**"not in today's screener top 10" in webhook response:**
- The screener hasn't run for today yet — wait for 1pm UTC or run `python3 crypto_agent.py fetch` manually

**Picks look like pump-and-dump tokens:**
- The pump guard filters coins up >60% in 7 days or >25% in 24 hours
- Also filtered: coins not listed on Binance.US
- If suspicious coins still appear, tighten `MAX_7D_CHANGE_PCT` in `crypto_agent.py`

---

## Part 3 — User Guide

### Your Morning Routine

**At 1pm UTC (8–9am US Eastern):**

Discord automatically receives:
1. **📊 Daily picks** — today's top coins with scores and rankings
2. **⚡ Strong Signals** (if any) — exceptional conditions worth watching closely

**What to do:**

```bash
python3 tv_integration.py watchlist --exchange BINANCE --filter-exchange BINANCE_US
```

Import the new watchlist into TradingView. Your existing alerts stay active — the new watchlist just updates which charts you're watching.

### Understanding Your Discord Messages

**Daily picks summary:**
```
📊 Daily picks — 2026-05-11
#1  SUIUSDT   score=+1.73
#2  SEIUSDT   score=+1.23
#3  ONDOUSDT  score=+1.07
#4  ENSUSDT   score=+1.01
```

**Strong signal (when present):**
```
⚡ Strong Signals — 2026-05-11
🔥 SUI — Exceptional Score
   Composite score 3.16 — momentum +3.77, relative strength +4.23
🏔️ ENS — Near ATH
   Only 12% below all-time high — breakout candidate
```

**Trade card (when a paper trade opens):**
```
🟢 PAPER BUY — SUIUSDT (trade #4)
$1.29 entry
Stop loss  $1.22  (-5.4%)
Take profit  $1.45  (+12.4%)
R:R 2.3:1 • Confidence 100%

What to do:
1️⃣ Open SUIUSDT on Binance.US
2️⃣ BUY $500 at market (~387 units)
3️⃣ Set stop-loss at $1.22
4️⃣ Set take-profit at $1.45
```

**Trade closed:**
```
✅ PAPER TRADE CLOSED — SUIUSDT (#4) — TARGET
Entry $1.29 → Exit $1.45
P&L +12.40% ≈ +$62
```

### What the Scores Mean

The composite score combines five factors:

| Factor | Weight | What it measures |
|---|---|---|
| Momentum | 35% | Price strength vs the rest of the market |
| Volume | 20% | Unusual volume relative to market cap |
| Volatility | 15% | Moving more than peers (opportunity) |
| Reversal | 15% | Distance from all-time low (recovery room) |
| Relative strength | 15% | Outperforming Bitcoin over 7 days |

- Score **> 0.5** → qualifies for trading
- Score **> 2.5** → exceptional, flagged as Strong Signal
- Score **> 3.0** → very rare, pay close attention

### When Signals Fire

The Pine Script indicator fires a webhook when all selected conditions are true at a 4H bar close:
- RSI was below 30 in the last 5 bars (oversold bounce)
- Price just crossed above VWAP (recovery confirmed)
- Volume is 1.5× the 20-bar average (buying conviction)
- EMA50 is above EMA200 (uptrend intact)

**Most days will be quiet.** The system looks for high-quality setups, not constant action. 1–3 signals per week across your watchlist is normal.

When a signal fires:
- TradingView sends it to your Railway server
- Server checks: is it in today's top 10? Is R:R ≥ 2:1? Are picks fresh?
- If all checks pass → paper trade opens → Discord trade card

### Reading Your Performance Report

```bash
python3 ai_trader.py report
```

Shows:
- **Hit rate** — what % of trades hit target vs stopped out
- **Avg P&L** — average return per trade
- **Weighted P&L** — accounts for position sizing
- **Excursion stats** — how far trades moved in your favour (MFE) before closing

A healthy system after 30+ days: hit rate above 50%, positive avg P&L.

Compare against BTC baseline in `crypto_agent.py report` — if your avg return beats BTC's return over the same period, the screener is adding real value.

---

## Quick Reference

| Task | Command |
|---|---|
| Run today's screener | `python3 crypto_agent.py fetch` |
| View today's picks + performance | `python3 crypto_agent.py report` |
| Export TradingView watchlist | `python3 tv_integration.py watchlist --exchange BINANCE --filter-exchange BINANCE_US` |
| View paper trade P&L | `python3 ai_trader.py report` |
| Close open paper trades | `python3 ai_trader.py evaluate` |
| Check weight refit data status | `python3 weight_refitter.py status` |
| Check server health | Open your Railway domain in a browser |

---

## Important Reminders

- **Paper trading only.** No real money moves until you wire a CCXT exchange connection — that's a separate deliberate step.
- **Validate for 30–60 days minimum** before considering live trading.
- **Secrets stay in Railway.** Never commit `WEBHOOK_AUTH_TOKEN`, `DISCORD_WEBHOOK_URL`, or any API keys to GitHub.
- **TradingView alerts can expire** — check the Alerts panel occasionally to confirm yours are still Active.
- **The screener runs at 1pm UTC** = 9am US Eastern (EDT) / 6am US Pacific (PDT).
- **BINANCE charts are fine for paper trading** on Binance.US — prices differ by <0.1%.
- **No broker signup needed in TradingView** — we use alerts only, not TradingView's trade execution.
