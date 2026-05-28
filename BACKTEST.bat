@echo off
echo ============================================================
echo   WOLFFS SCANNER (NEW)  —  Backtest Dashboard
echo ============================================================
echo.
echo Opening backtest dashboard on port 8504...
echo Dashboard: http://localhost:8504
echo (Original backtest stays on http://localhost:8502)
echo.
start "Wolffs Scanner Backtest" py -3.11 -m streamlit run backtest_dashboard.py --server.port 8504
echo.
pause
