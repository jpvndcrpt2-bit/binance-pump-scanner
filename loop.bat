@echo off
cd /d "%~dp0"
echo Scanning every 15 minutes. Close this window or press Ctrl+C to stop.
py pump.py --loop 15 --open
cmd /k
