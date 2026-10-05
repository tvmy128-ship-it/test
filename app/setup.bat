@echo off
rem DuoSkin Studio - setup. ASCII only, CRLF line endings.
rem Finds 64-bit Python 3.14 (preferred), 3.13 or 3.12, builds .venv, installs requirements\win-x64.lock (hashed, binary
rem only) and runs the doctor. Safe to run again at any time.
setlocal EnableExtensions
chcp 65001 >nul 2>&1
cd /d "%~dp0"
set "PYTHONUTF8=1"
set "PIP_DISABLE_PIP_VERSION_CHECK=1"
set "PIP_DEFAULT_TIMEOUT=60"
set "PYTHON_MANAGER_AUTOMATIC_INSTALL=0"
title DuoSkin Studio - setup
if not exist "tools\probe_python.py" goto :wrongdir
if not exist "requirements\win-x64.lock" goto :wrongdir
echo "%~dp0" | findstr /i /c:"onedrive" >nul 2>&1
if not errorlevel 1 call :onedrive

rem 1) Find 64-bit CPython 3.14 (target), 3.13 or 3.12 (all three install from the same lock).
rem    Every candidate is judged by tools\probe_python.py, which rejects the Store stub, 32-bit, ARM64 and free-threaded builds.
rem    Input is read from nul so that a Python install manager prompt can never hang this window.
set "PYEXE="
set "PYARG="
call :try "py" "-V:3.14"
call :try "py" "-V:3.13"
call :try "py" "-V:3.12"
rem    No py launcher (unticked in the python.org installer)? Look for python.exe directly.
call :try "python" ""
call :try "%LOCALAPPDATA%\Programs\Python\Python314\python.exe" ""
call :try "%LOCALAPPDATA%\Programs\Python\Python313\python.exe" ""
call :try "%LOCALAPPDATA%\Programs\Python\Python312\python.exe" ""
call :try "%ProgramFiles%\Python314\python.exe" ""
call :try "%ProgramFiles%\Python313\python.exe" ""
call :try "%ProgramFiles%\Python312\python.exe" ""
if not defined PYEXE call :pymanager
if not defined PYEXE goto :nopython
echo Using Python:
"%PYEXE%" %PYARG% --version

rem 2) Create or repair the virtual environment (a .venv copied from another PC, or whose Python was removed, is rebuilt).
if exist ".venv" if not exist ".venv\Scripts\python.exe" rmdir /s /q ".venv"
if exist ".venv\Scripts\python.exe" (
  ".venv\Scripts\python.exe" "tools\probe_python.py" <nul >nul 2>&1 || rmdir /s /q ".venv"
)
if exist ".venv\Scripts\python.exe" goto :venv_ok
echo Creating the virtual environment in .venv ...
"%PYEXE%" %PYARG% -m venv ".venv"
if errorlevel 1 goto :venvfail
:venv_ok

rem 3) Install the pinned, hashed, binary-only packages (about 240 MB to download, about 600 MB on disk).
rem    The bundled pip is new enough for every option used here, so it is not upgraded (one less download).
echo Installing packages. This can take several minutes, especially while your antivirus scans the new files.
".venv\Scripts\python.exe" -m pip install --require-hashes --no-deps --only-binary=:all: --find-links wheelhouse -r requirements\win-x64.lock
if not errorlevel 1 goto :installed
echo.
echo The exact install did not work. Trying again with the Windows certificate store and flexible versions...
".venv\Scripts\python.exe" "tools\install_deps.py"
if errorlevel 1 goto :installfailed
:installed
rem    start.bat compares this copy with requirements\win-x64.lock to notice an update.
copy /y "requirements\win-x64.lock" ".venv\win-x64.lock.installed" >nul

rem 4) Check the machine. Exit 0 = fine (missing kits and optional parts are only warnings). 2 = installed, but a check that
rem    blocks paid features failed (shown in the app). Anything else = broken install.
".venv\Scripts\python.exe" -m duoskin selfcheck
if errorlevel 1 goto :fail
".venv\Scripts\python.exe" -m duoskin doctor --setup
set "DOCTOR_RC=%errorlevel%"
if "%DOCTOR_RC%"=="0" goto :done
if "%DOCTOR_RC%"=="2" goto :warn
goto :fail
:warn
echo.
echo Setup finished, but the doctor found a problem that blocks paid features. Open DuoSkin Studio to see what to fix.
:done
echo.
echo Setup complete.
if not defined DUOSKIN_NOPAUSE pause
exit /b 0

:try
if defined PYEXE exit /b 0
"%~1" %~2 "tools\probe_python.py" <nul >nul 2>&1
if errorlevel 1 exit /b 0
set "PYEXE=%~1"
set "PYARG=%~2"
exit /b 0

:pymanager
where pymanager >nul 2>&1
if errorlevel 1 exit /b 0
echo Python 3.14 is not installed. Installing it with the Python install manager...
pymanager install 3.14
call :try "py" "-V:3.14"
exit /b 0

:onedrive
echo.
echo WARNING: this folder is inside OneDrive. OneDrive syncs and locks the many files that setup creates, which can make the
echo install fail or slow. Moving the folder to C:\DuoSkin\app first is much safer. Continuing anyway...
echo.
exit /b 0

:nopython
echo.
echo Could not find a suitable Python on this PC.
echo DuoSkin Studio needs 64-bit Python 3.14, 3.13 or 3.12 from python.org. The Microsoft Store "python" shortcut, 32-bit
echo Python, ARM64 Python and Python 3.11 or older do not work. Installing a new Python does not remove the old one.
echo Install "Python 3.14 - Windows installer 64-bit" from https://www.python.org/downloads/windows/
echo (choose Install Now; keep the py launcher ticked; no admin rights needed)
echo or run:  winget install 9NQ7512CXL7T -e --accept-package-agreements --accept-source-agreements
echo Then run setup.bat again.
if not defined DUOSKIN_NOPAUSE pause
exit /b 1

:wrongdir
echo.
echo This script must run from the extracted DuoSkin Studio folder (the one that contains tools and requirements).
echo Do not run it from inside the zip file or from a network share. Extract the zip first, then double-click setup.bat.
if not defined DUOSKIN_NOPAUSE pause
exit /b 1

:venvfail
echo.
echo Could not create the virtual environment. If the message above mentions ensurepip, run the Python installer
echo again, choose Modify, and tick "pip". Also make sure the folder is not read-only and not inside OneDrive.
goto :fail

:installfailed
echo.
echo Could not install the packages. Check your internet connection. Behind a company proxy, open a Command Prompt, run
echo   set HTTPS_PROXY=http://your-proxy:port
echo and start setup.bat from that same window. Antivirus "HTTPS scanning" can also block downloads.

:fail
echo.
echo Setup failed. The messages above say why. When it is fixed, run setup.bat again. doctor.bat checks the PC.
echo Logs (if any): %LOCALAPPDATA%\DuoSkin\logs
if not defined DUOSKIN_NOPAUSE pause
exit /b 1
