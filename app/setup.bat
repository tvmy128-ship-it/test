@echo off
rem DuoSkin Studio - setup. ASCII only, CRLF line endings.
setlocal EnableExtensions
cd /d "%~dp0"
set "PYTHONUTF8=1"
set "PIP_DISABLE_PIP_VERSION_CHECK=1"
title DuoSkin Studio - setup

rem 1) Find 64-bit CPython 3.14 (target) or 3.13 (fallback).
set "PY="
call :try "py -V:3.14"
if not defined PY call :try "py -V:3.13"
if not defined PY (
  where pymanager >nul 2>&1 && (
    echo Installing Python 3.14 with the Python install manager...
    py install 3.14
    call :try "py -V:3.14"
  )
)
if not defined PY goto :nopython
echo Using Python: %PY%

rem 2) Create or repair the virtual environment.
if exist ".venv\Scripts\python.exe" (
  ".venv\Scripts\python.exe" "tools\probe_python.py" >nul 2>&1 || rmdir /s /q ".venv"
)
if not exist ".venv\Scripts\python.exe" (
  %PY% -m venv .venv || goto :fail
)

rem 3) Install the pinned, hashed, binary-only packages.
".venv\Scripts\python.exe" -m pip install --require-hashes --no-deps --only-binary=:all: --find-links wheelhouse -r requirements\win-x64.lock
if errorlevel 1 goto :fail

rem 4) Check the machine. Exit 0 = fine (missing kits and optional parts are only warnings). 2 = installed, but a check that
rem    blocks paid features failed (shown in the app). 3 = broken install.
".venv\Scripts\python.exe" -m duoskin doctor --setup
if errorlevel 3 goto :fail
if errorlevel 2 echo Setup finished, but the doctor found a problem that blocks paid features. Open DuoSkin Studio to see what to fix.
echo Setup complete.
if not defined DUOSKIN_NOPAUSE pause
exit /b 0

:try
%~1 "tools\probe_python.py" >nul 2>&1
if not errorlevel 1 set "PY=%~1"
exit /b 0

:nopython
echo.
echo Could not find 64-bit Python 3.14 or 3.13 on this PC.
echo Install "Python 3.14 - Windows installer 64-bit" from https://www.python.org/downloads/windows/
echo or run:  winget install 9NQ7512CXL7T -e --accept-package-agreements
echo Then run setup.bat again.
if not defined DUOSKIN_NOPAUSE pause
exit /b 1

:fail
echo.
echo Setup failed. See the messages above. Logs: %LOCALAPPDATA%\DuoSkin\logs
if not defined DUOSKIN_NOPAUSE pause
exit /b 1
