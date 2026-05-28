@echo off
REM ── Force UTF-8 on Windows ──────────────────────────────────
chcp 65001 > nul
set PYTHONIOENCODING=utf-8

echo ============================================================
echo   FYERS DAILY LOGIN
echo ============================================================
echo.
echo Your browser will open. Log into Fyers.
echo Then paste the redirect URL back here.
echo.
py -3.11 core/fyers_auth.py
echo.
pause
