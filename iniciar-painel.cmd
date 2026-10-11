@echo off
cd /d "%~dp0"
python -m jev_bot dashboard
if errorlevel 1 pause
