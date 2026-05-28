@echo off
REM ── Force UTF-8 on Windows so emoji-prints in the Python code don't crash ─
chcp 65001 > nul
set PYTHONIOENCODING=utf-8

echo ============================================================
echo   WOLFFS SCANNER (NEW)  —  Starting
echo ============================================================
echo.

REM ── Speed knobs for quick same-day restarts ──────────────────
REM   Each is in HOURS. 0 = always run. 12 = skip if previous
REM   run finished cleanly less than 12 h ago (typical use).
REM   Markers live under data/run_cache/.
set DATA_INTEGRITY_MAX_AGE_HOURS=12
set SCANNER4_REPLAY_MAX_AGE_HOURS=12

REM ── History update knobs ─────────────────────────────────────
REM   Parallel workers — keep at 1 unless you've validated higher.
set HISTORY_UPDATE_WORKERS=1
REM   Emergency: skip the whole history-update step.
REM set HISTORY_UPDATE_SKIP=true

REM ── Carry-forward safety ─────────────────────────────────────
REM   DRY-RUN: print what carry-forward WOULD reset and exit
REM   without mutating state. Use this once after any structural
REM   change before going live.
REM set CARRY_FORWARD_DRY_RUN=true

REM ── Tick sanity (defaults are fine; uncomment to override) ───
REM set TICK_SANITY_JUMP_CHECK_ENABLED=true
REM set TICK_SANITY_OPENING_PCT_JUMP=25.0

echo Step 1: Starting Wolffs Scanner engine...
start "Wolffs Scanner Engine" cmd /k "chcp 65001 > nul & set PYTHONIOENCODING=utf-8 & set DATA_INTEGRITY_MAX_AGE_HOURS=%DATA_INTEGRITY_MAX_AGE_HOURS% & set SCANNER4_REPLAY_MAX_AGE_HOURS=%SCANNER4_REPLAY_MAX_AGE_HOURS% & set HISTORY_UPDATE_WORKERS=%HISTORY_UPDATE_WORKERS% & py -3.11 main.py"
echo.
echo Step 2: Waiting 8 seconds for engine to initialize...
timeout /t 8 /nobreak > nul
echo.
echo Step 3: Opening dashboard on port 8503...
start "Wolffs Scanner Dashboard" cmd /k "chcp 65001 > nul & set PYTHONIOENCODING=utf-8 & py -3.11 -m streamlit run dashboard.py --server.port 8503"
echo.
echo ============================================================
echo   Wolffs Scanner is running!
echo   Dashboard : http://localhost:8503
echo   (Original scanner stays on http://localhost:8501)
echo.
echo   Close both black windows to stop this scanner.
echo ============================================================
pause
