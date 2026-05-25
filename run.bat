@echo off
setlocal EnableDelayedExpansion
cd /d "%~dp0"
title SHOT — Screenshot Tool

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
        echo  Make sure Python 3 is installed: https://python.org/downloads
        echo  During install, tick "Add Python to PATH".
        echo.
        pause & exit /b 1
    )
)

:: Verify the venv isn't broken
".venv\Scripts\python.exe" --version >nul 2>&1
if errorlevel 1 (
    echo  [setup] Environment looks broken. Rebuilding...
    rmdir /s /q .venv
    py -3 -m venv .venv 2>nul || python -m venv .venv
    if errorlevel 1 (
        echo  ERROR: Rebuild failed. & pause & exit /b 1
    )
)

:: ── 2. Install / update Python packages ──────────────────────
echo  [setup] Checking Python packages...
".venv\Scripts\python.exe" -m pip install --upgrade pip --quiet
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 (
    echo.
    echo  ERROR: Package installation failed. See the output above for details.
    echo.
    pause & exit /b 1
)

:: ── 3. Install Playwright browser ────────────────────────────
echo  [setup] Checking Playwright browser (Chromium)...
".venv\Scripts\python.exe" -m playwright install chromium
if errorlevel 1 (
    echo.
    echo  ERROR: Could not install the Playwright browser.
    echo.
    pause & exit /b 1
)

:: ── 4. Already running? ──────────────────────────────────────
powershell -NoProfile -Command ^
    "if (Get-NetTCPConnection -LocalPort %PORT% -State Listen -ErrorAction SilentlyContinue) { exit 0 } else { exit 1 }" ^
    >nul 2>&1
if not errorlevel 1 (
    echo  Server is already running. Opening browser...
    start "" "%APP_URL%"
    exit /b 0
)

:: ── 5. Start server in a background window ───────────────────
echo.
echo  Starting server at %APP_URL%
echo  Keep the "SHOT - Server" window open while you use the tool.
echo  Close it (or this window) when you are done.
echo.
start "SHOT - Server" /D "%~dp0" ".venv\Scripts\python.exe" -m uvicorn main:app --host %HOST% --port %PORT%

:: ── 6. Wait for TCP port to open (30 second timeout) ─────────
::  Uses a raw socket connect, which is faster and more reliable
::  than Invoke-WebRequest. The server is ready the moment the
::  port accepts connections, even before the first HTTP response.
echo  Waiting for server to be ready...
powershell -NoProfile -Command ^
    "for ($i=0; $i -lt 120; $i++) {" ^
    "    try {" ^
    "        $c = New-Object Net.Sockets.TcpClient;" ^
    "        $c.Connect('127.0.0.1', %PORT%);" ^
    "        $c.Close(); exit 0" ^
    "    } catch {}" ^
    "    Start-Sleep -Milliseconds 250" ^
    "}; exit 1"

if errorlevel 1 (
    echo.
    echo  ERROR: Server did not start within 30 seconds.
    echo  Check the "SHOT - Server" window for details.
    echo  If that window closed immediately, try right-clicking this
    echo  file and choosing "Run as administrator".
    echo.
    pause & exit /b 1
)

:: ── 7. Open in default browser ───────────────────────────────
start "" "%APP_URL%"
echo  Ready! Opening %APP_URL% in your default browser.
