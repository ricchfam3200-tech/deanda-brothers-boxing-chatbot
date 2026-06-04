@echo off
echo.
echo ============================================================
echo   HOUSTON LEADS SETUP
echo ============================================================
echo.

REM Check for Python
python --version >nul 2>&1
if %errorlevel% neq 0 (
    echo ERROR: Python is not installed.
    echo.
    echo Download Python from: https://www.python.org/downloads/
    echo Make sure to check "Add Python to PATH" during install.
    echo.
    pause
    exit /b 1
)

echo Installing required libraries...
pip install requests pandas beautifulsoup4 openpyxl --quiet

echo.
echo Setup complete!
echo.
echo To run the lead engine, double-click RUN.bat
echo or type:  python houston_leads.py --streets sample_input.csv
echo.
pause
