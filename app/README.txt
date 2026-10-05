DuoSkin Studio
==============

DuoSkin Studio is a program that runs on your own Windows PC. You open it in your web browser. It helps you plan,
generate, check and package ORIGINAL Roblox duo skins: two matching blocky characters (boy + boy, girl + girl,
boy + girl or girl + boy) that clearly belong together but are never clones. You make every part yourself through the
app. Nothing is taken from the Roblox catalogue.

Nothing leaves your PC except the calls the app makes to the AI services you give it keys for.


WHAT YOU NEED
-------------
* Windows 10 or 11, 64-bit, with an internet connection.
* About 1 GB of free disk space (the install is about 600 MB).
* Python 3.14 or 3.13, 64-bit. If you do not have it, setup.bat tells you where to get it (python.org, or the
  Microsoft Store entry "Python Install Manager"). Do NOT use the "python" shortcut that opens the Microsoft Store.
* API keys (you add them inside the app, in Settings > Keys):
    - Anthropic (Claude)  - required
    - OpenAI (GPT Image)  - required
    - Tripo (3D)          - required for the full workflow
    - Recraft             - recommended
    - Gemini, fal         - optional
  No keys yet? Switch on Demo mode in Settings. Everything runs with pretend ("mock") providers, so you can look
  around. Demo results can never be exported.


INSTALL (first time)
--------------------
1. Download the zip file.
2. IMPORTANT: before you extract it, right-click the zip, choose Properties, tick "Unblock" at the bottom, and
   press OK. (Windows marks downloaded zips as "from the internet". If you skip this, SmartScreen can block the
   scripts inside, or Windows can refuse to run them.)
3. Extract everything to:   C:\DuoSkin\app
   Use a short path like this one. Do NOT extract into Downloads, Desktop, Documents or a OneDrive folder, and
   avoid folder names with spaces. Long paths and synced folders cause strange errors on Windows.
4. Double-click start.bat.
   The first time it installs what it needs. That can take several minutes, especially while your antivirus
   scans the new files. Let it finish. If Windows shows a blue "Windows protected your PC" box, click
   "More info" and then "Run anyway" (this only appears if step 2 was skipped).
5. Your browser opens at  http://127.0.0.1:8765/  (the app only listens on your own PC).
6. Open Settings and paste your keys. Use "Test key" to check each one. The Recraft test costs about $0.08 and
   says so before you press it.

Next time, just double-click start.bat again.


USING THE APP
-------------
* Keep the black console window open while you work. Closing it stops the app.
* Or press the Quit button in the page to stop it cleanly.
* The app asks before it spends more than the limits you set (default: $15 per duo, ask above $2 per step).
* If your PC goes to sleep during a long job, the app keeps it awake while work is running. If it is
  interrupted anyway (power cut, crash), start it again: unfinished steps resume, and paid jobs are never
  submitted twice.


WHERE YOUR FILES ARE
--------------------
* Your data (projects, pictures, settings, logs):  %LOCALAPPDATA%\DuoSkin
  Updating the app never deletes this folder.
* Finished kits and 3D packs you work with by hand:  %USERPROFILE%\DuoSkin Exports
* Your API keys are stored in Windows Credential Manager (or, if that is unavailable, in an encrypted file for
  your Windows account). They are never written to logs, exports or the diagnostics zip.


IF SOMETHING GOES WRONG
-----------------------
1. Double-click doctor.bat. It checks your PC and says in plain words what is wrong and what to do.
   OK = fine, WARN = optional part missing (the app still works), FAIL = fix this first.
   A fresh install shows a few WARN lines (no house style yet, no head base, no clone-check model). That is normal.
2. Common fixes:
   - "Python problem": install 64-bit Python 3.14 (or 3.13) from python.org, then run setup.bat again.
   - OpenCV or OCR fails on Windows "N" or "KN": install Microsoft's Media Feature Pack for your Windows version.
   - "DLL load failed": install the Microsoft Visual C++ 2015-2022 x64 redistributable (https://aka.ms/vs/17/release/vc_redist.x64.exe).
   - Secure connection errors: antivirus "HTTPS scanning" or a company proxy is re-signing traffic. Allow the app
     or add the proxy certificate to Windows.
   - Cannot write files: move the data folder out of OneDrive and allow DuoSkin in Windows Security >
     Ransomware protection > Controlled folder access.
   - The page does not open: type http://127.0.0.1:8765/ in the browser yourself. Use 127.0.0.1, not "localhost".
     If port 8765 is taken, the app picks the next free port and prints it in the console window.
3. Logs are in %LOCALAPPDATA%\DuoSkin\logs. In the app, Settings > Diagnostics makes a zip with the keys removed,
   which you can send to whoever is helping you.
4. Housekeeping from a console in this folder (replace .venv\Scripts\python.exe if you moved it):
     .venv\Scripts\python.exe -m duoskin gc --dry-run      shows what could be cleaned up (nothing is deleted)
     .venv\Scripts\python.exe -m duoskin gc                asks before deleting unused files
     .venv\Scripts\python.exe -m duoskin reset-leases      only if the app was killed and will not restart cleanly
                                                           (close the app first)


UPDATING
--------
Close the app. Download the new zip, Unblock it (step 2 above), and extract it over C:\DuoSkin\app. Run start.bat.
Your data folder is untouched.
