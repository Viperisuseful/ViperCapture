@echo off
setlocal EnableDelayedExpansion
cd /d "%~dp0"
title SHOT - Screenshot Tool

set "HOST=127.0.0.1"
set "PORT=8000"
set "APP_URL=http://%HOST%:%PORT%/"

echo.
echo  SHOT - Screenshot Tool
echo  ----------------------
echo.

:: ── 1. Create venv if missing ────────────────────────────────
if not exist ".venv\Scripts\python.exe" (
    echo  [setup] Creating Python environment (first run only)...
    py -3 -m venv .venv 2>nul || python -m venv .venv
    if errorlevel 1 (
        echo.
        echo  ERROR: Could not create a Python environment.
        echo  Install Python 3 from: https://python.org/downloads
        echo  During install, check "Add Python to PATH".
        echo.
        pause
        exit /b 1
    )
)

:: Verify venv isn't broken
".venv\Scripts\python.exe" --version >nul 2>&1
if errorlevel 1 (
    echo  [setup] Environment is broken. Rebuilding...
    rmdir /s /q .venv
    py -3 -m venv .venv 2>nul || python -m venv .venv
    if errorlevel 1 (
        echo  ERROR: Rebuild failed.
        pause
        exit /b 1
    )
)

:: ── 2. Install packages ───────────────────────────────────────
echo  [setup] Checking Python packages...
".venv\Scripts\python.exe" -m pip install --upgrade pip --quiet
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 (
    echo.
    echo  ERROR: Package installation failed. See above for details.
    echo.
    pause
    exit /b 1
)

:: ── 3. Playwright browser ─────────────────────────────────────
echo  [setup] Checking Playwright browser...
".venv\Scripts\python.exe" -m playwright install chromium
if errorlevel 1 (
    echo.
    echo  ERROR: Could not install the Playwright browser.
    echo.
    pause
    exit /b 1
)

:: ── 4. Already running? ───────────────────────────────────────
powershell -NoProfile -Command "try { $t = New-Object Net.Sockets.TcpClient; $t.Connect('127.0.0.1', %PORT%); $t.Close(); exit 0 } catch { exit 1 }" >nul 2>&1
if not errorlevel 1 (
    echo  Server already running. Opening browser...
    start "" "%APP_URL%"
    goto :end
)

:: ── 5. Start server in a new window ──────────────────────────
echo.
echo  Starting server...
echo  Leave the "SHOT - Server" window open while using the tool.
echo.
start "SHOT - Server" ".venv\Scripts\python.exe" -m uvicorn main:app --host %HOST% --port %PORT%

:: ── 6. Wait for port to open (1 check per second, 30s max) ───
echo  Waiting for server to be ready...
set /a attempts=0

:waitloop
powershell -NoProfile -Command "try { $t = New-Object Net.Sockets.TcpClient; $t.Connect('127.0.0.1', %PORT%); $t.Close(); exit 0 } catch { exit 1 }" >nul 2>&1
if not errorlevel 1 goto :ready
timeout /t 1 /nobreak >nul
set /a attempts+=1
if %attempts% lss 30 goto :waitloop

echo.
echo  ERROR: Server did not start within 30 seconds.
echo  Check the "SHOT - Server" window for error details.
echo  If that window closed immediately, try running as Administrator.
echo.
pause
exit /b 1

:ready
:: ── 7. Open browser ───────────────────────────────────────────
start "" "%APP_URL%"
echo  Ready! Opened %APP_URL% in your default browser.

:end
echo.
echo  This window can be closed.
echo.
pause
