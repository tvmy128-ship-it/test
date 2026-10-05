@echo off
rem DuoSkin Studio - start. ASCII only, CRLF line endings.
setlocal EnableExtensions
cd /d "%~dp0"
set "PYTHONUTF8=1"
set "DUOSKIN_NOPAUSE=1"
if not exist ".venv\Scripts\python.exe" goto :setup
".venv\Scripts\python.exe" -m duoskin selfcheck >nul 2>&1 || goto :setup
goto :run
:setup
call "%~dp0setup.bat"
if errorlevel 1 goto :fail
:run
set "DUOSKIN_NOPAUSE="
title DuoSkin Studio - close this window to stop
".venv\Scripts\python.exe" -m duoskin run --open-browser
if errorlevel 1 goto :fail
exit /b 0
:fail
echo.
echo DuoSkin Studio stopped with an error.
echo Logs: %LOCALAPPDATA%\DuoSkin\logs
pause
exit /b 1
