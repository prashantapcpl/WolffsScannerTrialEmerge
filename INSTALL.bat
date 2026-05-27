@echo off
echo ============================================================
echo   FYERS SCANNER — Installing Libraries
echo ============================================================
echo.
py -3.11 -m pip install fyers-apiv3 streamlit flask pandas numpy requests pytz schedule websocket-client
echo.
echo ============================================================
echo   Done! Now edit config.json with your Fyers credentials.
echo   Then run LOGIN.bat and START.bat.
echo ============================================================
pause
