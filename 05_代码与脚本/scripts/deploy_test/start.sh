#!/bin/bash
echo "Starting Binance API test..."
python test_binance.py
echo "Test completed. Sleeping to keep container alive..."
sleep 300