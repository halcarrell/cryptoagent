#!/bin/bash
# Daily local routine — run this each morning.
cd "$(dirname "$0")"

echo "=== [1/7] Fetching today's picks ==="
python3 crypto_agent.py fetch

echo ""
echo "=== [2/7] Evaluating realized returns (past picks) ==="
python3 crypto_agent.py evaluate

echo ""
echo "=== [3/7] Checking open paper trades via Binance 4H candles (stops/targets) ==="
python3 ai_trader.py evaluate

echo ""
echo "=== [4/7] Portfolio risk state ==="
python3 risk_monitor.py status

echo ""
echo "=== [5/7] Generating TradingView watchlist ==="
python3 tv_integration.py watchlist --exchange BINANCE --filter-exchange US_EXCHANGES

echo ""
echo "=== [6/7] Today's picks + performance ==="
python3 crypto_agent.py report

echo ""
echo "=== [7/7] Paper trade P&L ==="
python3 ai_trader.py report

echo ""
echo "Done. Import the watchlist file into TradingView."
