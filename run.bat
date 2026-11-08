@echo off
rem Run VoiceMeeter Volume OSD with a console window (useful for diagnostics).
cd /d "%~dp0"
python main.py %*
if errorlevel 1 pause
