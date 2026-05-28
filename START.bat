@echo off
echo ============================================================
echo   WOLFFS SCANNER (NEW)  —  Starting
echo ============================================================
echo.

REM ── Performance knobs ────────────────────────────────────────
REM   The "fast-skip" path makes same-day restarts finish in ~1 min by
REM   skipping API calls for symbols whose CSV is already current.
REM   Parallel workers add more speed but the Fyers SDK isn't fully
REM   thread-safe — leave at 1 unless you've validated 2/4 works.
set HISTORY_UPDATE_WORKERS=1
REM   Set HISTORY_UPDATE_SKIP=true ONLY if you're sure CSVs are already fresh.
REM set HISTORY_UPDATE_SKIP=true

REM ── Tick sanity (defaults are fine; uncomment to override) ───
REM set TICK_SANITY_JUMP_CHECK_ENABLED=true
REM set TICK_SANITY_OPENING_PCT_JUMP=25.0

echo Step 1: Starting Wolffs Scanner engine...
start "Wolffs Scanner Engine" py -3.11 main.py
echo.
echo Step 2: Waiting 8 seconds for engine to initialize...
timeout /t 8 /nobreak > nul
echo.
echo Step 3: Opening dashboard on port 8503...
start "Wolffs Scanner Dashboard" py -3.11 -m streamlit run dashboard.py --server.port 8503
echo.
echo ============================================================
echo   Wolffs Scanner is running!
echo   Dashboard : http://localhost:8503
echo   (Original scanner stays on http://localhost:8501)
echo.
echo   Close both black windows to stop this scanner.
echo ============================================================
pause
