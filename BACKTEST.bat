@echo off
echo ============================================================
echo   FYERS RSI SCANNER — Backtest Dashboard
echo ============================================================
echo.
echo Opening backtest dashboard on port 8502...
echo Dashboard: http://localhost:8502
echo.
start "Backtest Dashboard" py -3.11 -m streamlit run backtest_dashboard.py --server.port 8502
echo.
pause
