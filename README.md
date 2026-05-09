# Crypto Screener Agent — TradingView + AI Trading Pipeline

A scheduled agent that fetches crypto market data, scores assets, surfaces top 10 picks, integrates with TradingView for chart confirmation, and runs paper trades through an AI decision layer.

## File map

|File                        |Purpose                                                             |
|----------------------------|--------------------------------------------------------------------|
|`crypto_agent.py`           |Daily fetch, scoring, snapshots, realized-return tracking           |
|`tv_integration.py`         |Export picks as TradingView watchlist + Pine Script score lookup    |
|`screener_confirmation.pine`|Pine Script v5 indicator: confirms entries, fires webhook alerts    |
|`webhook_server.py`         |Flask server that receives TradingView alerts                       |
|`ai_trader.py`              |Decision function (rules-based + optional Claude LLM) + paper ledger|

All four Python files share `crypto_agent.db`.

## Install

```bash
pip install requests flask
# optional, only if using LLM-based decisions:
pip install anthropic
```

## The full daily flow

```
                    ┌──────────────────────┐
   Cron 13:00 UTC → │  crypto_agent fetch  │ → snapshots + top 10 picks → DB
                    └──────────────────────┘
                                ↓
                    ┌──────────────────────┐
                    │ tv_integration.py    │ → watchlist.txt for TradingView
                    │   (watchlist + pine) │ → scores.pine snippet
                    └──────────────────────┘
                                ↓
                    ┌──────────────────────┐
   You import       │ TradingView charts   │ ← Pine Script applied per chart
   the watchlist →  │  + screener_*.pine   │
                    └──────────────────────┘
                                ↓ (technical confirmation fires)
                    ┌──────────────────────┐
                    │  webhook_server.py   │ ← TradingView alert (JSON)
                    │  (Flask, port 8080)  │
                    └──────────────────────┘
                                ↓
                    ┌──────────────────────┐
                    │  ai_trader.decide()  │ → cross-ref screener + R:R + size
                    │  rules OR LLM        │
                    └──────────────────────┘
                                ↓
                    ┌──────────────────────┐
   Cron evaluates → │  paper_trades table  │ → P&L tracked over time
                    └──────────────────────┘
```

## Step-by-step

### 1. Run the screener daily

```bash
python crypto_agent.py fetch
python crypto_agent.py evaluate
python crypto_agent.py report
```

### 2. Export today’s picks for TradingView

```bash
python tv_integration.py watchlist --exchange BINANCE
# produces watchlist_2026-05-09_binance.txt
```

In TradingView: open Watchlist → menu (…) → **Import list** → select the file. You now have today’s picks as a watchlist.

### 3. Add the Pine Script indicator

- Open `screener_confirmation.pine`, copy contents
- TradingView → Pine Editor → paste → Save → “Add to chart”
- For each pick, set the **Manual score** input from your daily report. Or run `python tv_integration.py pine`, paste the generated `get_score()` function into the indicator, and replace `score := score_manual` with `score := get_score(syminfo.ticker)` to auto-load scores.

The indicator paints the chart green when the score qualifies and drops a `LONG` label when RSI bounce + VWAP reclaim + volume spike + EMA50/200 trend all confirm.

### 4. Run the webhook server

```bash
export WEBHOOK_AUTH_TOKEN="$(openssl rand -hex 32)"
python webhook_server.py
```

For TradingView to reach a server on your machine, expose it:

```bash
ngrok http 8080
# copy the https://xxxx.ngrok-free.app URL
```

### 5. Wire TradingView alerts (paid plan required)

On each chart with the indicator:

- Right-click → Add alert
- Condition: “Screener Confirmation” → “any alert() function call”
- Webhook URL: `https://your-ngrok-url/webhook`
- Message: leave blank — the Pine Script generates the JSON payload
- Set the indicator’s **Webhook auth token** input to match `WEBHOOK_AUTH_TOKEN`

When the indicator fires, TradingView POSTs JSON → Flask server → `ai_trader.decide_trade()` → paper trade opens if approved.

### 6. Track paper trade performance

Add to your daily cron after `crypto_agent fetch`:

```bash
python ai_trader.py evaluate    # closes positions on stop/target hits
python ai_trader.py report      # hit rate, avg P&L, weighted P&L
```

## AI decision logic

`ai_trader.decide_trade()` rejects the trade unless **all** of these hold:

1. Symbol is in **today’s screener top 10**
1. Screener composite score ≥ 0.5
1. Risk/reward ≥ 2:1
1. Alert payload has valid entry/stop/target

Position sizing scales with confidence: `size_pct = MAX_POSITION_PCT × min(1.0, score)`, capped at 5%.

### Plugging in Claude for richer reasoning

Set environment vars before starting the webhook server:

```bash
export USE_LLM_DECIDER=true
export ANTHROPIC_API_KEY=sk-ant-...
```

`decide_trade_with_llm()` sends the alert + screener context to Claude and parses a structured JSON decision back. Same interface, but the LLM can catch things the rules miss — “this just broke a 90-day range,” “score is high but driven entirely by one factor,” etc.

## Tuning checklist

- **Risk parameters** in `ai_trader.py`: `MIN_SCORE_TO_TRADE`, `MAX_POSITION_PCT`, `MIN_RISK_REWARD`
- **Confirmation filters** in the Pine Script: toggle RSI, VWAP, volume, trend
- **ATR multipliers** for stop/target — wider in high-vol regimes, tighter in chop
- **Score weights** in `crypto_agent.py` — refit from realized-return data after ~30 days

## Hard caveats

- TradingView **webhooks require a paid plan** (Essential or above).
- Pine Script `alert()` calls fire at bar close on the chart timeframe — alert frequency = chart timeframe.
- This is **paper trading only**. Wiring real execution is intentionally not included; validate for at least 30–60 days first.
- The webhook server is minimally hardened. For production: HTTPS, rotate auth token, rate-limit, reverse proxy.
- “AI trading” here means structured rule application plus optional LLM reasoning. Neither predicts the market — both enforce discipline you’ve defined.
- Crypto can gap through stops. Real fills will differ from paper fills.
- Not financial advice.

## Where to extend next

1. Replace `decide_trade()` rules with a learned model (logistic regression / XGBoost on factor scores → realized 3d return).
1. Add a Telegram/Discord notifier when a paper trade opens.
1. Per-trade journaling: capture chart screenshot URL with each alert.
1. Multi-strategy: have the LLM choose long/short/flat based on regime.
1. When ready for live trading, replace `open_paper_trade()` with an exchange API call (CCXT supports 100+ exchanges with one interface). Start with tiny size.