# Crypto Screener Agent — Complete Guide

## What Does This System Do?

Every day your system automatically:
1. **Scans** the top 250 cryptocurrencies and scores them on momentum, volume, trend, and relative strength
2. **Filters** out pump tokens (>60% 7d / >25% 24h moves), stablecoins, near-$1 pegged tokens, and coins not available on any US exchange
3. **Picks** the top coins and posts them to Discord
4. **Flags** exceptional signals — unusual volume, near all-time highs, top scores
5. **Watches** TradingView charts all day for technical confirmations
6. **Receives** alerts from TradingView when a signal fires
7. **Decides** whether to open a paper trade (checks R:R, screener score, data freshness)
8. **Pings Discord** with a trade card when a paper trade opens or closes
9. **Self-tests** the live server daily and posts a health report to Discord

> **This is paper trading only.** No real money moves. Validate for 30–60 days before considering live trading.

---

## Accounts You Need

| Service | What For | Cost |
|---|---|---|
| **GitHub** | Stores the code | Free |
| **Railway** | Runs the server 24/7 | ~$5/month |
| **TradingView** | Charts + signal alerts | Essential plan ($15/mo minimum) — webhooks require a paid plan |
| **Discord** | Trade notifications | Free |
| **Binance.US** | Primary US exchange | Free to open |
| **Coinbase** | Secondary US exchange (more coins available) | Free to open |

---

## Part 1 — Setup Guide

### Step 1 — Get the Code on GitHub

```bash
cd "/Users/halcarrell/Documents/Claude/Projects/Crypto research/files"
git remote add origin https://github.com/YOUR_USERNAME/cryptoagent.git
git push -u origin main
```

### Step 2 — Deploy to Railway

1. Go to **railway.com** → New Project → Deploy from GitHub → select `cryptoagent`
2. Railway auto-detects Python and builds
3. Click your service → **Settings** → **Networking** → port `8080` → **Generate Domain**

**Environment variables** (Settings → Variables):

| Variable | Value |
|---|---|
| `CRYPTO_AGENT_DB` | `/data/crypto_agent.db` |
| `WEBHOOK_AUTH_TOKEN` | Run `openssl rand -hex 32` in Terminal — paste the result |
| `DISCORD_WEBHOOK_URL` | Your Discord webhook URL (see Step 3) |
| `PORTFOLIO_USD` | Your paper trading budget e.g. `10000` |
| `COINGECKO_API_KEY` | *(optional but recommended)* Free Demo API key — see note below |
| `TIINGO_API_KEY` | *(optional)* Free API key from [api.tiingo.com](https://api.tiingo.com) — enables ticker-specific news headlines in Discord trade cards and daily picks. Falls back to free RSS feeds (CoinDesk/CoinTelegraph/Decrypt) when not set. |

> **CoinGecko free Demo key:** go to [coingecko.com](https://coingecko.com) → sign up free → API → Demo → copy key. Raises the rate limit from ~10 to 30 req/min and prevents 429 errors during the daily fetch. No credit card needed.

**Add a volume** (Settings → Volumes):
- Add Volume → Mount path: `/data`
- This is where your database lives — persists through restarts

**Verify:**
Open your Railway domain in a browser:
```json
{"status": "ok", "service": "crypto-screener-webhook", "decider": "rules"}
```

### Step 3 — Set Up Discord

1. Discord → any channel → gear ⚙ → **Integrations** → **Webhooks** → **New Webhook**
2. Copy the webhook URL → paste into Railway as `DISCORD_WEBHOOK_URL`

> Never share this URL publicly.

### Step 4 — Run the First Screener

```bash
cd "/Users/halcarrell/Documents/Claude/Projects/Crypto research/files"
python3 crypto_agent.py fetch
python3 crypto_agent.py report
```

Takes 2–3 minutes. Shows today's top 10 picks when done.

### Step 5 — Set Up TradingView

**Add the Pine Script indicator (one-time only):**
1. TradingView → open any chart
2. Click **Pine Editor** at the bottom
3. Clear existing code → paste the full contents of `screener_confirmation.pine`
4. Click **Save** → **Add to chart**
5. Click the ⚙ gear on the indicator → set **Webhook auth token** to your `WEBHOOK_AUTH_TOKEN` from Railway → **OK**

> The Pine Script never needs editing. The server handles all score/regime checking. Short signals are also embedded — the server decides whether to act on them.

**Mobile alert format:** TradingView push notifications now show a human-readable summary:
```
🟢 LONG INJUSDT @ 7.10  SL -5.1%  TP +15.3%  R:R 3.0:1  RSI 45.2
```
The JSON payload for the server is attached after the first line — you never see it.

**Import today's watchlist:**

```bash
python3 tv_integration.py watchlist --exchange BINANCE --filter-exchange US_EXCHANGES
```

This creates `watchlist_YYYY-MM-DD_binance.txt` filtered to coins available on Binance.US or Coinbase.

In TradingView: Watchlist panel → **⋯** → **Import list** → select the file.

> Always use watchlist import — never type symbols manually. The file specifies the exact exchange (`BINANCE:INJUSDT`) so there's no ambiguity.

**Create alerts (one per coin, done once per coin):**
1. Open each chart from the watchlist → set timeframe to **4H**
2. Right-click chart → **Add alert**
3. Condition: `Screener Confirmation` → `any alert() function call`
4. **Notifications** tab → check **Webhook URL** → paste:
   ```
   https://YOUR-SERVICE.up.railway.app/webhook
   ```
5. Click **Create**

> One alert covers both long and short signals. Alerts are permanent — only re-create if you add new coins.

**TradingView plan note:** Webhooks require Essential plan ($15/mo) or higher. The webhook field appears on free plans but never fires.

### One-Click Execute

**From Discord (fastest for mobile):**
Every trade card now includes a **[🚀 Execute on Binance.US →]** link. Tapping it opens Binance.US directly to the trading pair with the entry price visible. You then place the order manually in 2 taps.

**From TradingView charts (native broker integration):**
TradingView supports direct order placement when connected to Coinbase Advanced Trade:
1. TradingView → Profile → Trading Panel → Connect Broker → **Coinbase**
2. Authorise with your Coinbase account
3. The chart shows a Buy/Sell panel — you can place orders without leaving TradingView

> Binance.US is not yet a TradingView broker partner. Use the Discord link or the Binance.US app directly for Binance.US trades.

**Future: fully automated execution** — when ready for live trading, set `BINANCE_API_KEY` and `BINANCE_API_SECRET` in Railway. The server will place real orders automatically when a paper trade opens (coming soon).

---

## Part 2 — Admin Guide

### What Runs Automatically

| When | What Happens |
|---|---|
| Daily 1pm UTC | Screener fetches 250 coins, applies pump guard, posts picks to Discord |
| Daily 1pm UTC | ⚡ Strong Signals alert fires if exceptional conditions detected |
| Daily 1pm UTC | Health check email sent (if configured) |
| Daily 2pm UTC | Automated test agent runs 5 live endpoint checks, posts pass/fail to Discord |
| Sunday 2pm UTC | Weight refitter runs, posts results to Discord |
| Monday 3pm UTC | Weekly enhancement agent reviews code, researches new data sources, posts report to Discord |
| Always on | Webhook server receives TradingView alerts 24/7 |

Everything runs inside the single Railway web service — no separate cron jobs needed.

### Is Everything Healthy?

**Server health:**
```
https://YOUR-SERVICE.up.railway.app/
```
Should return `{"status": "ok"}`.

**Railway logs:**
Railway dashboard → your service → **Logs** tab → look for:
```
[scheduler] Started — daily@13:00 UTC
DB init OK
Webhook server starting on :8080
```

**Today's picks:**
```bash
python3 crypto_agent.py report
```

### Email Health Reports (Optional)

Add to Railway variables:

| Variable | Value |
|---|---|
| `EMAIL_FROM` | Your Gmail address |
| `EMAIL_TO` | Recipient (can be same as FROM) |
| `EMAIL_APP_PASSWORD` | Gmail App Password — see steps below |

**To generate a Gmail App Password:**
1. Make sure **2-Step Verification** is enabled on your Google account: [myaccount.google.com/security](https://myaccount.google.com/security) → *How you sign in to Google* → *2-Step Verification*
2. Once 2SV is on, go to: [myaccount.google.com/apppasswords](https://myaccount.google.com/apppasswords)
3. Click **Create** → name it "Crypto Screener" → copy the 16-character password
4. Paste that password (no spaces) as `EMAIL_APP_PASSWORD` in Railway

> If the App Passwords page redirects you away, 2-Step Verification is not yet enabled — complete step 1 first.

### Tuning the Screener

Edit `config.json`:

```json
{
  "strong_signals": {
    "score_exceptional": 2.5,   ← lower to see more strong alerts
    "volume_surge_score": 3.0,  ← lower to catch more volume spikes
    "ath_within_pct": 20.0      ← raise to widen the ATH proximity window
  },
  "risk": {
    "min_risk_reward_gross": 2.0,  ← minimum R:R before fees
    "min_risk_reward_net":   1.6,  ← minimum R:R after 0.1% round-trip fees
    "max_position_pct":      5.0,  ← max % of portfolio per trade
    "min_score":             1.2   ← minimum screener score to trade (z-score scale)
  }
}
```

Push to GitHub — Railway redeploys automatically.

### Pump Guard

The screener automatically drops coins that have already pumped:
- >60% move in 7 days
- >25% move in 24 hours
- Relative strength z-score >5

These are late-stage pumps — entry risk outweighs opportunity.

### Stablecoin Filter

Beyond name/symbol matching (USD, USDT, USDC, etc.), the screener also drops any coin where:
- Price is between $0.90 and $1.10 **and**
- 30-day price change is less than 3%

This catches rebasing and pegged tokens that don't have "USD" in their name (e.g. RUSD, similar tokens).

### Exchange Filtering

Picks are filtered to coins tradeable on **US exchanges** — Binance.US or Coinbase. A coin passes if it's available on either platform. Discord and the watchlist file both use this filter automatically.

**Standard daily command (use this):**
```bash
python3 tv_integration.py watchlist --exchange BINANCE --filter-exchange US_EXCHANGES
```

**To see all picks without any filter:**
```bash
python3 tv_integration.py watchlist --exchange BINANCE
```

**To restrict to Binance.US only:**
```bash
python3 tv_integration.py watchlist --exchange BINANCE --filter-exchange BINANCE_US
```

### Applying New Score Weights (After 30+ Days)

```bash
python3 weight_refitter.py status    # check data sufficiency
python3 weight_refitter.py validate  # walk-forward test: do new weights improve results?
python3 weight_refitter.py refit     # generate weights.json
```

Review `weights.json` before manually updating `WEIGHTS` in `crypto_agent.py`.

### Common Problems

| Symptom | Fix |
|---|---|
| 🔴 Cron failure — fetch failed: 429 | CoinGecko rate limit hit. The screener will retry automatically (up to 4×, waits 60s between attempts). To prevent it permanently, add a free `COINGECKO_API_KEY` in Railway (see Setup). |
| Server returns 502 | Check Railway logs; confirm volume attached; try redeploy |
| TradingView webhook 404 | URL has trailing slash — must end with `/webhook` not `/webhook/` |
| TradingView webhook 401 | Auth token in indicator settings doesn't match Railway `WEBHOOK_AUTH_TOKEN` |
| No Discord pings | Confirm `DISCORD_WEBHOOK_URL` set in Railway; check logs for `[notifier]` errors |
| "not in today's screener top 10" | Picks stale — wait for 1pm UTC or run `python3 crypto_agent.py fetch` |
| Watchlist has only 2-3 coins | Normal on some days — Binance.US + Coinbase combined still excludes obscure tokens |
| Pump tokens in picks | Pump guard is active (60%/7d, 25%/24h) — if still appearing, lower thresholds in `crypto_agent.py` |
| Stablecoin or pegged token in picks | Price-stability filter should catch it — check if price is near $1 with <3% 30d move |
| No paper trades opening | Check `min_score` in `config.json` (currently 1.2) — if everything is "pass", screener score may be below threshold |
| Alerts disappeared in TradingView | Re-create them — alerts occasionally expire or get removed |
| Daily test agent not posting | Check claude.ai conversations list for the agent run output; confirm GitHub repo is public |

---

## Part 3 — User Guide

### Morning Routine (Takes 2 Minutes)

**Discord will ping you automatically at 1pm UTC** with:
- Today's top picks and scores
- ⚡ Strong Signals (if any)

Run the single morning command to fetch fresh picks and generate the watchlist file:
```bash
./morning.sh
```
Or manually in two steps:
```bash
python3 crypto_agent.py fetch
python3 tv_integration.py watchlist --exchange BINANCE --filter-exchange US_EXCHANGES
```
> Always run `fetch` first — `tv_integration.py` reads from your local database and will repeat stale data if you skip it.
Import the new file into TradingView.

### Understanding Your Discord Messages

**Daily picks** (filtered to US-tradeable coins only):
```
📊 Daily picks — 2026-05-13
#1  INJUSDT   score=+3.09
#2  TIAUSDT   score=+1.18
#3  AKTUSDT   score=+1.15
```

**Strong signal:**
```
⚡ Strong Signals — 2026-05-13
🔥 INJ — Exceptional Score
   Composite score 3.09 — momentum +2.87, relative strength +4.12
🏔️ TIA — Near ATH
   Only 14% below all-time high — breakout candidate
```

**Trade card when a paper trade opens:**
```
🟢 PAPER BUY — INJUSDT (trade #5)
$5.09 entry
Stop loss   $4.82  (-5.3%)
Take profit $5.90  (+15.9%)
R:R 3.0:1  •  Confidence 100%

What to do:
1️⃣  Open INJUSDT on Binance.US (or Coinbase)
2️⃣  BUY $500 at market (~98 units)
3️⃣  Set stop-loss at $4.82
4️⃣  Set take-profit at $5.90
```

**Trade closed:**
```
✅ PAPER TRADE CLOSED — INJUSDT (#5) — TARGET
Entry $5.09 → Exit $5.90
P&L +15.9% ≈ +$80
```

### What the Scores Mean

| Factor | Weight | What it measures |
|---|---|---|
| Momentum | 35% | Price strength vs the rest of the market |
| Volume | 20% | Unusual volume relative to market cap |
| Volatility | 15% | Moving more than peers |
| Reversal | 15% | Distance from all-time low |
| Relative strength | 15% | Outperforming Bitcoin over 7 days |

- Score **> 1.2** → qualifies for trading
- Score **> 2.5** → exceptional — flagged as Strong Signal
- Score **> 3.0** → very rare, pay close attention

> Scores are cross-sectional z-scores — a score of +1.2 means the coin is outperforming ~88% of the universe on composite factors that day.

### When Signals Fire

The Pine Script fires on a 4H bar close when:
- RSI was below 30 in the last 5 bars *(oversold bounce)*
- Price crossed back above VWAP *(recovery confirmed)*
- Volume is 1.5× the 20-bar average *(conviction)*
- EMA50 > EMA200 *(uptrend intact)*

**Most days are quiet.** 1–3 signals per week is normal and expected. The server then checks: is it in today's picks? Is R:R good? Are picks fresh (< 36 hours old)?

**If signals are too rare:** uncheck **RSI bounce** in the indicator settings — this is the most restrictive filter in strong bull markets where coins don't dip to RSI 30.

### Performance Report

```bash
python3 ai_trader.py report
```

Shows hit rate, average P&L, excursion stats (MFE/MAE), and decider activity.

Compare your avg return against the BTC baseline in `python3 crypto_agent.py report`. If your picks beat BTC over 30+ samples, the screener is adding real value.

---

## Quick Reference

| Task | Command |
|---|---|
| Run today's screener | `python3 crypto_agent.py fetch` |
| View picks + performance | `python3 crypto_agent.py report` |
| **Morning routine (fetch + watchlist)** | **`./morning.sh`** |
| Export watchlist (US exchanges) | `python3 tv_integration.py watchlist --exchange BINANCE --filter-exchange US_EXCHANGES` |
| View paper trade P&L | `python3 ai_trader.py report` |
| Close open paper trades | `python3 ai_trader.py evaluate` |
| Check weight refit data | `python3 weight_refitter.py status` |
| Run full daily job manually | `python3 crypto_agent.py daily` |
| Check server health | Open your Railway domain in browser |

---

## Architecture Overview

```
Railway Web Service (always on)
├── Flask webhook server — receives TradingView alerts
│   └── Risk monitor (pre-trade circuit breaker) — blocks trades that exceed
│       exposure limits, hit correlation thresholds, or breach loss-streak rules
├── Background scheduler
│   ├── Daily 13:00 UTC → crypto_agent.daily() → Discord + email
│   ├── Sunday 14:00 UTC → weight_refitter.refit() → Discord
│   └── Every 4H @ :15 → evaluate_open_trades_live() → Discord close cards
└── Shared SQLite volume at /data/crypto_agent.db

TradingView
└── Pine Script v6 indicator (static — never changes)
    └── 4H bar close → webhook → Flask → risk check → decide → Discord

Claude.ai Scheduled Agents
├── Daily 14:00 UTC → 5 live endpoint tests → Discord pass/fail
└── Monday 15:00 UTC → code review + data source research → Discord report

Discord
├── Daily picks summary (with paper trading stats + 7d P&L)
├── ⚡ Strong signals
├── 🟢 Trade opened card (with news headlines)
├── ✅/🛑 Trade closed card
├── 🛡 Risk block alert (when circuit breaker fires)
├── 🔴 Cron failure alert
├── Daily tests pass/fail
└── 🔍 Weekly enhancement report
```

### Scoring Factors (6 total)

| Factor | Default Weight | What it measures |
|---|---|---|
| Momentum | ~21% | Price strength vs the rest of the market |
| Volume | ~25% | Unusual volume relative to market cap |
| Volatility | ~21% | Moving more than peers |
| Reversal | ~19% | Distance from all-time low |
| Relative strength | ~4% | Outperforming Bitcoin over 7 days |
| **Decorrelation** | **10%** | **Low 30d correlation with BTC — independent move** |

> Weights are loaded from `weights.json` (written by the weekly refitter) when available.
> The decorrelation factor is always injected at 10%, with the other 5 factors renormalized to 90%.

---

## Important Reminders

- **Paper trading only.** No real money moves until you deliberately wire a CCXT exchange connection.
- **Validate 30–60 days minimum** before considering live trading.
- **Never commit secrets.** `WEBHOOK_AUTH_TOKEN`, `DISCORD_WEBHOOK_URL`, API keys → Railway only.
- **TradingView alerts stay active permanently** — only re-create if you add new coins.
- **No daily Pine Script updates needed** — the script is static; the server handles score logic.
- **Screener runs at 1pm UTC** = 9am US Eastern / 6am US Pacific.
- **BINANCE charts are fine** for paper trading on Binance.US — price difference < 0.1%.
- **No broker signup in TradingView** — we use alerts only, not TradingView's trade execution.
- **For live trading:** coins on Binance.US → trade there; coins only on Coinbase → trade there.
