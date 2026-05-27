@echo off
echo ============================================================
echo   FYERS RSI SCANNER — Running Backtest
echo ============================================================
echo.
echo This window will show backtest progress.
echo DO NOT CLOSE this window until you see:
echo "Backtest complete!"
echo.
echo Starting backtest...
echo.
py -3.11 backtester/run_backtest.py
echo.
echo ============================================================
echo   Done! Go back to the backtest dashboard to see results.
echo   Refresh the dashboard page in your browser.
echo ============================================================
pause
