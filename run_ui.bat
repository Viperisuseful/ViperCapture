@echo off
setlocal

cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo Creating virtual environment...
    py -3 -m venv .venv 2>nul || python -m venv .venv
)

".venv\Scripts\python.exe" --version >nul 2>&1
if errorlevel 1 (
    echo Existing virtual environment is invalid. Recreating...
    rmdir /s /q .venv
    py -3 -m venv .venv 2>nul || python -m venv .venv
)

echo Activating environment...
call ".venv\Scripts\activate.bat"
if errorlevel 1 (
    echo Failed to activate virtual environment.
    pause
    exit /b 1
)

echo Installing dependencies...
python -m pip install --upgrade pip >nul
python -m pip install -r requirements.txt
if errorlevel 1 (
    echo Dependency installation failed.
    pause
    exit /b 1
)

echo Ensuring Playwright Chromium is installed...
python -m playwright install chromium
if errorlevel 1 (
    echo Playwright browser installation failed.
    pause
    exit /b 1
)

set "HOST=127.0.0.1"
set "PORT=8000"

powershell -NoProfile -Command "if (Get-NetTCPConnection -LocalPort 8000 -State Listen -ErrorAction SilentlyContinue) { exit 0 } else { exit 1 }"
if not errorlevel 1 (
    echo Server already running at http://%HOST%:%PORT%/
    start "" "http://%HOST%:%PORT%/"
    exit /b 0
)

echo Starting local screenshot UI at http://%HOST%:%PORT%/
start "Screenshot Server" ".venv\Scripts\python.exe" -m uvicorn main:app --host %HOST% --port %PORT%

powershell -NoProfile -Command "for ($i=0; $i -lt 120; $i++) { try { $r = Invoke-WebRequest -Uri 'http://%HOST%:%PORT%/' -UseBasicParsing -TimeoutSec 2; if ($r.StatusCode -eq 200) { exit 0 } } catch {} ; Start-Sleep -Milliseconds 250 }; exit 1"
if errorlevel 1 (
    echo Server did not become ready in time.
    echo Check the "Screenshot Server" window for errors.
    pause
    exit /b 1
)

start "" "http://%HOST%:%PORT%/"
echo UI opened in your default browser.
