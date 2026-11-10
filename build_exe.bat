@echo off
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo Project virtual environment not found. Create it and install requirements first.
    exit /b 1
)

".venv\Scripts\python.exe" -m pip install -r requirements.txt pyinstaller
if errorlevel 1 exit /b 1

".venv\Scripts\python.exe" -m PyInstaller --noconfirm --clean --onefile --windowed --name TreadmillLogger treadmill_logger.py
if errorlevel 1 exit /b 1

echo.
echo Build complete: dist\TreadmillLogger.exe
endlocal
