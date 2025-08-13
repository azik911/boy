@echo off
cd /d "%~dp0"
if exist ".venv\Scripts\activate.bat" call ".venv\Scripts\activate.bat"

REM API
start cmd /k "cd web\src && uvicorn main:app --host 127.0.0.1 --port 8001"

REM Bot
start cmd /k "cd bot && python bot.py"
