@echo off
echo.
echo ============================================================
echo   HOUSTON WHOLESALE LEAD ENGINE
echo ============================================================
echo.
echo Running lead pull for all target streets...
echo This will take 5-10 minutes. Do not close this window.
echo.

python houston_leads.py --streets sample_input.csv --out final_leads.csv

echo.
echo ============================================================
echo   Done! Open final_leads.csv to see your leads.
echo ============================================================
echo.
pause
