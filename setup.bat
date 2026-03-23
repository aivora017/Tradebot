@echo off
echo.
echo ========================================================
echo   WAR ROOM BOT — Setup (Windows)
echo ========================================================
echo.

python --version >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Python not found. Install from https://python.org
    pause
    exit /b
)

if not exist "venv" (
    echo Creating virtual environment...
    python -m venv venv
)

call venv\Scripts\activate
echo Installing dependencies...
pip install -r requirements.txt --quiet

if not exist ".env" (
    copy .env.example .env
    echo.
    echo [!] .env file created. Fill in your API keys before running.
)

if not exist "logs" mkdir logs
if not exist "data" mkdir data

echo.
echo ========================================================
echo   Setup complete!
echo.
echo   DAILY ROUTINE:
echo   1. python get_token.py     (fresh Upstox token)
echo   2. Update KEY_LEVELS in config.py
echo   3. python main.py          (starts bot)
echo   4. ngrok http 5003         (browser dashboard)
echo ========================================================
echo.
pause
