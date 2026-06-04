@echo off
REM Launch the Holdem server. Open http://localhost:8000 in your browser.
cd /d "%~dp0"
py -m uvicorn server:app --host 0.0.0.0 --port 8000
pause
