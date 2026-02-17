@echo off
echo Building RA Bridge
echo ==================
echo.

cd /d "%~dp0"

REM Close ra_bridge.exe if running (otherwise build fails with PermissionError)
taskkill /f /im ra_bridge.exe >nul 2>&1
if exist "dist\ra_bridge.exe" (
    timeout /t 2 /nobreak >nul
)

REM Auto-increment version by 0.01 (always)
python bump_version.py

python --version >nul 2>&1
if errorlevel 1 (
    echo ERROR: Python not found!
    pause
    exit /b 1
)

python -c "import PyInstaller" >nul 2>&1
if errorlevel 1 (
    echo Installing PyInstaller...
    pip install pyinstaller
)

pip install pyserial >nul 2>&1
pip install pygame >nul 2>&1

echo.
echo Building RA Bridge...
echo.

set PYARGS=--onefile --windowed --name "ra_bridge" --clean --add-data "nes-esp-firmware\data\games.txt;nes-esp-firmware\data" --add-data "misc\webapp;webapp" --add-data "misc\achievement_alert.html;misc" --hidden-import serial.tools.list_ports
if exist "nes-esp-firmware\data\everdrive_games.txt" set PYARGS=%PYARGS% --add-data "nes-esp-firmware\data\everdrive_games.txt;nes-esp-firmware\data"

pyinstaller %PYARGS% everdrive_bridge_gui.py

if errorlevel 1 (
    echo Build failed!
    echo.
    echo If "Access is denied" - close RA Bridge if it is running, then try again.
    pause
    exit /b 1
)

echo.
echo ====================================
echo Build complete!
echo.
echo Executable: dist\ra_bridge.exe
echo.
echo Launching app...
start "" "dist\ra_bridge.exe"
pause
