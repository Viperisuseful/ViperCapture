@echo off
cd /d "%~dp0"
if exist ".venv\Scripts\python.exe" ( ".venv\Scripts\python.exe" launch.py ) else ( python launch.py 2>nul || py -3 launch.py )
pause
