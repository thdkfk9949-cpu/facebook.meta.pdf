@echo off
REM Run the audit and open the report. Double-click this file.
REM Everything here is read-only: the tool only sends GET requests.
setlocal
cd /d "%~dp0"

if not exist ".env" (
  echo .env is missing.
  echo Copy .env.example to .env and put your token and ad account id in it.
  pause
  exit /b 2
)

echo Checking credentials...
py -m metaaudit --env-file .env --check-auth
if errorlevel 1 (
  echo.
  echo Setup check failed. Fix what it reported above, then run this again.
  pause
  exit /b 3
)

for /f %%i in ('powershell -NoProfile -Command "Get-Date -Format yyyy-MM-dd"') do set TODAY=%%i

echo.
echo Running audit...
py -m metaaudit --env-file .env --save-snapshot "snapshots\%TODAY%.json" --format markdown --out "out\audit-%TODAY%.md"
if errorlevel 1 (
  echo.
  echo The audit failed. The error above says what to fix.
  pause
  exit /b 3
)

echo.
echo Report   : out\audit-%TODAY%.md
echo Snapshot : snapshots\%TODAY%.json   ^(upload this one for analysis^)
start "" notepad "out\audit-%TODAY%.md"
pause
