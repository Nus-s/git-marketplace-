@echo off
chcp 65001 >nul
echo Starting Marketplace Fulfillment & Flash Sale API on http://localhost:8000/docs ...
cd /d "%~dp0"
start "" "http://localhost:8000/docs"
"C:\Users\777\AppData\Local\Programs\Python\Python312\python.exe" -m uvicorn app.main:app --host 0.0.0.0 --port 8000
pause
