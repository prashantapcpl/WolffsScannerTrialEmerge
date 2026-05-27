@echo off
echo ============================================================
echo   FYERS RSI SCANNER — Starting
echo ============================================================
echo.
echo Step 1: Starting scanner engine...
start "Fyers Scanner Engine" py -3.11 main.py
echo.
echo Step 2: Waiting 8 seconds for engine to initialize...
timeout /t 8 /nobreak > nul
echo.
echo Step 3: Opening dashboard...
start "Fyers Dashboard" py -3.11 -m streamlit run dashboard.py --server.port 8501
echo.
echo ============================================================
echo   Scanner is running!
echo   Dashboard: http://localhost:8501
echo   Close both black windows to stop the scanner.
echo ============================================================
pause
