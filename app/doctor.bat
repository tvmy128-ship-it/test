@echo off
rem DuoSkin Studio - doctor. ASCII only, CRLF line endings.
setlocal EnableExtensions
chcp 65001 >nul 2>&1
cd /d "%~dp0"
set "PYTHONUTF8=1"
if not exist ".venv\Scripts\python.exe" goto :notinstalled
".venv\Scripts\python.exe" -m duoskin doctor
set "DOCTOR_RC=%errorlevel%"
echo.
pause
exit /b %DOCTOR_RC%
:notinstalled
echo.
echo DuoSkin Studio is not installed yet. Double-click setup.bat (or start.bat) first.
pause
exit /b 1
