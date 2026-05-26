#!/bin/bash
# Daily local routine — run this each morning.
cd "$(dirname "$0")"

echo "=== [1/5] Fetching today's picks ==="
python3 crypto_agent.py fetch

echo ""
echo "=== [2/5] Evaluating realized returns ==="
python3 crypto_agent.py evaluate

echo ""
echo "=== [3/5] Generating TradingView watchlist ==="
python3 tv_integration.py watchlist --exchange BINANCE --filter-exchange US_EXCHANGES

echo ""
echo "=== [4/5] Today's picks + performance ==="
python3 crypto_agent.py report

echo ""
echo "=== [5/5] Paper trade P&L ==="
python3 ai_trader.py report

echo ""
echo "Done. Import the watchlist file into TradingView."
