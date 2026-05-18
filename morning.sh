#!/bin/bash
# Run this each morning to get today's picks and fresh watchlist file.
cd "$(dirname "$0")"

echo "Fetching today's picks..."
python3 crypto_agent.py fetch

echo ""
echo "Generating TradingView watchlist..."
python3 tv_integration.py watchlist --exchange BINANCE --filter-exchange US_EXCHANGES

echo ""
echo "Today's picks:"
python3 crypto_agent.py report | head -20
