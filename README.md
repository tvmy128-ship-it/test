# DuoSkin Studio

A local Windows web app that plans, builds, checks and packages **original Roblox duo skins**: two coordinated blocky
characters (boy + boy, girl + girl, boy + girl) that clearly belong together but are never clones. Nothing comes from the Roblox
catalogue. The app prepares the files and an upload checklist; you upload in Roblox Studio yourself.

- **Run it:** see `app/README.txt` (Windows install, keys, demo mode). Demo mode needs no keys.
- **What exists, what does not, what to test first:** `docs/AS_BUILT.md`.
- **Design:** `docs/APP_SPEC.md` (build contract), `docs/PROMPT_BIBLE.md` (every prompt), `docs/FAILURE_MODES.md` (checks and
  thresholds), `docs/PROPOSAL_DECISION.md` (the approved design decisions that keep duos varied), `docs/DuoSkin_Workflow_Summary.pdf`.
- **Develop:** `cd app`, create a venv, install `requirements/win-x64.lock` (Windows) or `pip install -e .[dev]`, then
  `python -m pytest tests --ignore=tests/web --ignore=tests/e2e_ui`. Browser tests need Chromium (Playwright).
