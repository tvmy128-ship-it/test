@echo off
rem DuoSkin Studio - doctor. ASCII only, CRLF line endings.
cd /d "%~dp0"
set "PYTHONUTF8=1"
".venv\Scripts\python.exe" -m duoskin doctor
pause
