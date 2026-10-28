@echo off
cd /d "%~dp0"
echo 1) collect  2) study  3) backtest --grid
py antipump.py collect
py antipump.py study
py antipump.py backtest --grid
cmd /k
