@echo off
setlocal
cd /d "%~dp0"
title SCP - Local Test Server

rem This launcher is only for local testing. Machine-only settings from
rem .env.local (including GPU flags) are still loaded by the app.
set "SHOT_HOSTED=0"

echo.
echo   Starting SCP locally at http://127.0.0.1:8000/
echo   Press Ctrl+C to stop the server.
echo.

if exist ".venv\Scripts\python.exe" (
  ".venv\Scripts\python.exe" launch.py
) else (
  where py >nul 2>&1
  if not errorlevel 1 (
    py -3 launch.py
  ) else (
    where python >nul 2>&1
    if errorlevel 1 (
      echo   ERROR: Python 3 was not found. Install Python and try again.
      set "EXIT_CODE=1"
      goto :done
    )
    python launch.py
  )
)

set "EXIT_CODE=%ERRORLEVEL%"

:done
if not "%EXIT_CODE%"=="0" echo   SCP exited with code %EXIT_CODE%.
echo.
pause
exit /b %EXIT_CODE%
