@echo off
cd /d "%~dp0"
if exist "dist\ra_bridge.exe" (
    "dist\ra_bridge.exe"
    exit /b %errorlevel%
)
python everdrive_bridge_gui.py
