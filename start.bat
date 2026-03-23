@echo off
title WAR ROOM — STARTUP
color 0A
cls

echo.
echo ========================================================
echo   ⚡  WAR ROOM — FULL AUTO STARTUP
echo ========================================================
echo.

:: ── Kill any existing instances ───────────────────────────
echo [0/4] Killing any existing bot/ngrok instances...
taskkill /F /FI "WINDOWTITLE eq WAR ROOM BOT*" >nul 2>&1
taskkill /F /FI "WINDOWTITLE eq NGROK TUNNEL*" >nul 2>&1
taskkill /F /IM ngrok.exe >nul 2>&1
:: Kill any python processes running main.py
for /f "tokens=2" %%a in ('tasklist /FI "IMAGENAME eq python.exe" /NH 2^>nul') do (
    wmic process where "ProcessId=%%a and CommandLine like '%%main.py%%'" delete >nul 2>&1
)
timeout /t 2 /nobreak >nul
echo       Done.

:: ── Activate venv ─────────────────────────────────────────
cd /d "%~dp0"
call "%~dp0venv\Scripts\activate.bat"
if errorlevel 1 (
    echo [ERROR] venv not found. Run setup.bat first.
    pause & exit /b
)

:: ── Step 1: Token + Morning Setup ─────────────────────────
echo.
echo [1/4] Refreshing Upstox token + auto-setup...
echo       (Browser will open. Login and paste redirect URL.)
echo.
python get_token.py
if errorlevel 1 (
    echo [ERROR] Token refresh failed. Exiting.
    pause & exit /b
)

:: ── Step 2: Start main.py in new window ───────────────────
echo.
echo [2/4] Starting WAR ROOM bot...
start "WAR ROOM BOT" cmd /k "cd /d "%~dp0" && call venv\Scripts\activate.bat && python main.py"

:: Wait for Flask bridge to come up
echo       Waiting for bot to initialize...
timeout /t 6 /nobreak >nul

:: ── Step 3: Start ngrok in new window ─────────────────────
echo.
echo [3/4] Starting ngrok tunnel...
start "NGROK TUNNEL" cmd /k "ngrok http 5003"

:: Wait for ngrok to get a URL
echo       Waiting for ngrok to connect...
timeout /t 5 /nobreak >nul

:: ── Step 4: Get ngrok URL and open dashboard ──────────────
echo.
echo [4/4] Fetching ngrok URL and opening dashboard...

for /f "delims=" %%U in ('powershell -NoProfile -Command "try { $j=(Invoke-WebRequest http://127.0.0.1:4040/api/tunnels -UseBasicParsing).Content | ConvertFrom-Json; $j.tunnels[0].public_url } catch { 'NOT_FOUND' }"') do set NGROK_URL=%%U

if "%NGROK_URL%"=="NOT_FOUND" (
    echo [WARN] Could not auto-detect ngrok URL.
    echo        Open Chrome manually and go to your ngrok URL.
) else (
    echo       ngrok URL: %NGROK_URL%
    echo       Opening dashboard in Chrome...
    start chrome "%NGROK_URL%"
)

echo.
echo ========================================================
echo   ✅  ALL SYSTEMS STARTED
echo   Dashboard: %NGROK_URL%
echo   Bot:       Running in "WAR ROOM BOT" window
echo   ngrok:     Running in "NGROK TUNNEL" window
echo   To RESTART: just run start.bat again
echo ========================================================
echo.
pause
