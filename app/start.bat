@echo off
rem DuoSkin Studio - start. ASCII only, CRLF line endings.
setlocal EnableExtensions
chcp 65001 >nul 2>&1
cd /d "%~dp0"
set "PYTHONUTF8=1"
set "DUOSKIN_NOPAUSE=1"
if not exist "setup.bat" goto :wrongdir
if not exist ".venv\Scripts\python.exe" goto :setup
rem A zip extracted over an old install can carry a new lock: setup.bat keeps a copy of the lock it installed in .venv.
fc /b "requirements\win-x64.lock" ".venv\win-x64.lock.installed" >nul 2>&1
if errorlevel 1 goto :setup
".venv\Scripts\python.exe" -m duoskin selfcheck >nul 2>&1
if errorlevel 1 goto :setup
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
echo DuoSkin Studio stopped with an error. The messages above say why.
echo Logs: %LOCALAPPDATA%\DuoSkin\logs   Checks: doctor.bat   Help: README.txt
pause
exit /b 1
:wrongdir
echo.
echo This script must run from the extracted DuoSkin Studio folder. Do not run it from inside the zip file: right-click
echo the zip, choose Extract All, then double-click start.bat in the new folder (see README.txt).
pause
exit /b 1
