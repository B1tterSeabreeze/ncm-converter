@echo off
cd /d "%~dp0"

:: Try python from PATH first
where pythonw >nul 2>&1
if %errorlevel%==0 (
    start "" pythonw ncm_converter.py
    exit /b
)

:: Fallback: try common Python install paths
if exist "C:\Python312\pythonw.exe" (
    start "" "C:\Python312\pythonw.exe" ncm_converter.py
    exit /b
)

if exist "%LOCALAPPDATA%\Programs\Python\Python312\pythonw.exe" (
    start "" "%LOCALAPPDATA%\Programs\Python\Python312\pythonw.exe" ncm_converter.py
    exit /b
)

:: Last resort: use python (will show a console window)
python ncm_converter.py
