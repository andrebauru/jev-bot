@echo off
rem Abre a interface gráfica do JEv Bot (paper trading).
cd /d %~dp0
python -m jev_bot gui
pause
