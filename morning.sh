#!/bin/bash
# Daily local routine — run this each morning.
# Railway handles: session scanner (every 15min, 12-18 UTC), live eval (every 4H),
# daily screener (13:00 UTC), self-test (14:00 UTC), trade eval (21:00 UTC).
# This script covers what only makes sense locally: status review + watchlist.
cd "$(dirname "$0")"

echo "=== [1/8] Fetching today's picks ==="
echo "    (30d momentum, BTC correlations, refitted weights)"
python3 crypto_agent.py fetch

echo ""
echo "=== [2/8] Evaluating realized returns (past picks) ==="
python3 crypto_agent.py evaluate

echo ""
echo "=== [3/8] Checking open paper trades via Binance 4H candles ==="
python3 ai_trader.py evaluate

echo ""
echo "=== [4/8] Portfolio risk state + recent rejections ==="
echo "    (exposure, loss streak, MDD circuit breaker, recent signal blocks)"
python3 risk_monitor.py report

echo ""
echo "=== [5/8] Weight refit data check ==="
echo "    (data sufficiency for Sunday's auto-refit)"
python3 weight_refitter.py status

echo ""
echo "=== [6/8] Generating TradingView watchlist ==="
echo "    (filtered to US-tradeable coins; also used by session scanner)"
python3 tv_integration.py watchlist --exchange BINANCE --filter-exchange US_EXCHANGES

echo ""
echo "=== [7/8] Today's picks + screener performance ==="
python3 crypto_agent.py report

echo ""
echo "=== [8/8] Paper trade P&L + Surprise Ratio breakdown ==="
echo "    (EDGE = setup worked | EXPECTED = normal variance | LUCK/ANOMALY = discount)"
python3 ai_trader.py report

echo ""
echo "Done."
echo ""
echo "Notes:"
echo "  • Session scanner fires automatically 12:00-18:00 UTC — no manual entry needed"
echo "  • Regime check: hit /analysis?auth=TOKEN on Railway for score-bucket advice"
echo "  • Import watchlist file into TradingView if watchlist coins changed today"
