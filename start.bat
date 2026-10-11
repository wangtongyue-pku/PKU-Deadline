@echo off
chcp 65001 > nul
cd /d "%~dp0"
title Deadline

echo ============================================
echo   Deadline  -  http://127.0.0.1:8502
echo ============================================
echo.
echo   This window IS the service. Closing it stops the app.
echo   To stop: press Ctrl+C, or just close this window.
echo.
echo   Tip: run this from the PowerShell window that already
echo        has your API keys set, or the AI parsing page will
echo        report that no key is configured.
echo.

start "" http://127.0.0.1:8502
.\.venv\Scripts\python.exe -m streamlit run app.py --server.port 8502 --server.headless true

echo.
echo Service stopped.
pause
