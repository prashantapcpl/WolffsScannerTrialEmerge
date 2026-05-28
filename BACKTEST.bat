@echo off
REM ── Force UTF-8 on Windows ──────────────────────────────────
chcp 65001 > nul
set PYTHONIOENCODING=utf-8

echo ============================================================
echo   WOLFFS SCANNER (NEW)  —  Backtest Dashboard
echo ============================================================
echo.
echo Opening backtest dashboard on port 8504...
echo Dashboard: http://localhost:8504
echo (Original backtest stays on http://localhost:8502)
echo.
start "Wolffs Scanner Backtest" cmd /k "chcp 65001 > nul & set PYTHONIOENCODING=utf-8 & py -3.11 -m streamlit run backtest_dashboard.py --server.port 8504"
echo.
pause
