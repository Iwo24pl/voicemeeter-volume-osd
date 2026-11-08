@echo off
rem Build a single-file executable in dist\VoiceMeeterPopup.exe
cd /d "%~dp0"
python -m pip install --upgrade -r requirements.txt pyinstaller
if errorlevel 1 goto :error
python -m PyInstaller --noconfirm --clean --onefile --windowed ^
  --name VoiceMeeterPopup ^
  --icon assets\icon.ico ^
  --add-data "assets;assets" ^
  --hidden-import pystray._win32 ^
  main.py
if errorlevel 1 goto :error
echo.
echo Built dist\VoiceMeeterPopup.exe
pause
exit /b 0

:error
echo.
echo Build failed.
pause
exit /b 1
