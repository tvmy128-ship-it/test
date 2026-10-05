DuoSkin Studio
==============

DuoSkin Studio runs on your own Windows PC and opens in your web browser. It helps you plan, make, check and package
ORIGINAL Roblox duo skins: two matching blocky characters (boy + boy, girl + girl, boy + girl) that clearly belong together
but are never clones. Nothing is copied from the Roblox catalogue, and you upload the finished kit to Roblox yourself.

Nothing leaves your PC except the calls the app makes to the AI services you give keys for.


WHAT YOU NEED
-------------
* Windows 10 or 11, 64-bit, and an internet connection (the first start downloads about 240 MB of packages).
* About 1 GB of free disk space.
* 64-bit Python 3.14, 3.13 or 3.12 from python.org. If it is missing, setup.bat tells you exactly what to install.
  Do NOT use the "python" shortcut that opens the Microsoft Store. You do not need administrator rights.
* API keys (see "GETTING YOUR KEYS" below). You can look around without any: use Demo mode.


INSTALL (first time)
--------------------
1. Download the zip file.
2. IMPORTANT, before you extract: right-click the zip, choose Properties, tick "Unblock" at the bottom, press OK.
   (Windows marks downloaded files as "from the internet". Without this, SmartScreen may block the scripts.)
3. Extract everything to   C:\DuoSkin\app
   A short folder like this one avoids Windows' long-path errors. Do NOT use Downloads, Desktop, Documents or any
   OneDrive folder, and avoid spaces and accents in the folder name. If you cannot create C:\DuoSkin, use
   C:\Users\<your name>\DuoSkin\app instead.
4. Double-click start.bat. The first time it sets itself up, which takes several minutes while your antivirus scans
   the new files. Let it finish. If a blue "Windows protected your PC" box appears, click "More info", then "Run
   anyway" (this only happens when step 2 was skipped).
5. Your browser opens at http://127.0.0.1:8765/ (the app listens on your own PC only; use 127.0.0.1, not "localhost").
6. Follow the Setup page, or open Settings > Keys and paste your keys.

Next time, just double-click start.bat again.


GETTING YOUR KEYS
-----------------
Paste each key into Settings > Keys, then press "Test key". Keys are billed by the service, not by this app. The app asks
before it spends more than your limits (default: $15 per duo, ask above $2 per step).
* Anthropic (Claude), required:  console.anthropic.com > Settings > API Keys > Create Key. Add some credit first.
* OpenAI (GPT Image), required:  platform.openai.com/api-keys > Create new secret key. Image models may ask you to
  verify your organization in the OpenAI settings first.
* Tripo (3D), needed for the full 3D workflow:  platform.tripo3d.ai > API Keys (the key starts with tsk_).
* Recraft, recommended:  recraft.ai > your profile > API. The "Test key" button for Recraft costs about $0.08 and says so
  first.
* Gemini and fal, optional:  aistudio.google.com/apikey and fal.ai/dashboard/keys.

DEMO MODE: no keys yet? Turn on Settings > Providers > "Demo mode". Every service is replaced by a practice stand-in, so
nothing is charged and you can click through the whole flow. A banner stays at the top, and demo results can never be
exported. Turn it off again when your keys are in.


USING THE APP
-------------
* Keep the black console window open while you work. Closing it stops the app. The Quit button in the page stops it
  cleanly. If the console asks "Terminate batch job (Y/N)?", press Y.
* Do not click inside the console window and then leave it paused; if it looks frozen, press Enter in it once.
* If your PC goes to sleep during a long job the app keeps it awake while work runs. If a job is interrupted anyway (power
  cut, crash), start the app again: unfinished steps resume, and paid jobs are never submitted twice.


WHERE YOUR FILES ARE
--------------------
* Your data (projects, pictures, settings, logs):  %LOCALAPPDATA%\DuoSkin
  Updating the app never touches this folder. Type that path into Explorer's address bar to open it.
* Finished kits and 3D packs:  %USERPROFILE%\DuoSkin Exports
* API keys: Windows Credential Manager (entries ending in @DuoSkinStudio), or an encrypted file for your Windows account
  in the data folder. Keys are never written to logs, exports or the diagnostics zip.


IF SOMETHING GOES WRONG
-----------------------
1. Double-click doctor.bat. It checks your PC and says in plain words what is wrong and what to do.
   OK = fine, WARN = an optional part is missing (the app still works), FAIL = fix this first.
   A fresh install shows a few WARN lines (no house style yet, no head base, no clone-check model). That is normal.
2. Common fixes:
   - "Could not find a suitable Python": install 64-bit Python 3.14 from python.org (choose Install Now), then run
     setup.bat again. 32-bit, ARM and Microsoft Store Pythons do not work.
   - Setup cannot download packages: check your internet. Behind a company proxy, open Command Prompt, type
     set HTTPS_PROXY=http://your-proxy:port   and run setup.bat from that same window.
   - "Secure connection" errors: antivirus "HTTPS scanning" or a company proxy is re-signing traffic. Allow the app,
     or add the proxy's certificate to Windows.
   - "DLL load failed": install the Microsoft Visual C++ 2015-2022 x64 redistributable
     (https://aka.ms/vs/17/release/vc_redist.x64.exe).
   - OpenCV fails on Windows "N" or "KN" editions: install Microsoft's Media Feature Pack for your Windows version.
   - Cannot write files: move the data folder out of OneDrive and allow DuoSkin in Windows Security > Ransomware
     protection > Controlled folder access.
   - The page does not open: type http://127.0.0.1:8765/ yourself. If port 8765 is busy, the app picks the next free
     port and prints it in the console window.
3. Logs are in %LOCALAPPDATA%\DuoSkin\logs. Settings > Diagnostics makes a zip with the keys removed, which you can send
   to whoever is helping you.
4. Housekeeping, from a Command Prompt in this folder:
     .venv\Scripts\python.exe -m duoskin gc --dry-run      shows what could be cleaned up (nothing is deleted)
     .venv\Scripts\python.exe -m duoskin gc                asks before deleting unused files
     .venv\Scripts\python.exe -m duoskin reset-leases      only if the app was killed and will not restart cleanly
                                                           (close the app first)


UPDATING
--------
Close the app. Download the new zip, Unblock it (step 2 above) and extract it over C:\DuoSkin\app. Run start.bat: it
notices the change and updates what it needs. Your data folder is untouched.


UNINSTALL
---------
1. Close the app.
2. Delete the folder C:\DuoSkin (the program and its packages).
3. Optional, only if you want your work gone too: delete %LOCALAPPDATA%\DuoSkin (projects, pictures, logs) and
   %USERPROFILE%\DuoSkin Exports (finished kits).
4. Optional: Control Panel > Credential Manager > Windows Credentials, and remove the entries ending in @DuoSkinStudio
   (your saved keys). You can also use the Remove button next to each key in Settings > Keys before you delete the app.
Python itself is not touched; remove it in Settings > Apps if you no longer need it.
