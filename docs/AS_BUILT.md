# DuoSkin Studio: as built (status, deviations, what is not done)

Written 2026-10-06 for the code in `app/`. The three design docs (`APP_SPEC.md`, `PROMPT_BIBLE.md`, `FAILURE_MODES.md`, all v1.3)
say what the app should be. This file says what exists, where the code differs from those docs, and what has **not** been
tested. When this file and the docs disagree, this file describes the code.

## 1. What exists

A local web app for Windows (Python backend on 127.0.0.1, vanilla-JS browser UI) that plans, builds, checks and packages original
Roblox duo skins. It runs completely in **demo mode with no API keys**, and the whole flow has been clicked through in a real
browser: brief, plan (3 plans, one labelled wildcard), Gate 1 concept, part board, build (Tripo API mode and manual 3D mode),
duo checks, Gate 3 pick, export.

| Area | Where | State |
|---|---|---|
| Server, SQLite, job engine (leases, recovery, budget gate, SSE) | `duoskin/{engine,db,api}`, `config.py`, `security.py` | built, tested |
| Keys in Windows Credential Manager (DPAPI file fallback), keys never logged | `keystore.py`, `logsetup.py` | built, tested on Linux only |
| Providers: Claude, GPT Image, Recraft, Tripo, Gemini (+ mocks, fault injection) | `duoskin/providers` | built, tested against mocks only |
| Plan loop (analyst, taste profile, planner, linter C1, critic, reviser, wildcard) | `duoskin/pipeline/plan.py`, `checks/plan_rules.py`, `prompts/` | built, tested in mock mode |
| Gate 1 concept (approve / reimagine / change-by-text, palette lock, design card) | `pipeline/concept.py`, `change.py` | built |
| Part board (hair, face, accessory, shirt, pants, colours, each alone) | `pipeline/{parts,assetloop,face,hair,accessory,clothing,colours}.py` | built (reduced modes, see section 3) |
| Clothing: 585x559 template, 15 garment recipes, fabrics, folds, painted shoes | `roblox/template.py`, `imaging/compositor.py` | built; the 18 regions match the official PNGs pixel for pixel |
| 3D: import, repair, validate, hair registration, slabs, renderer, Tripo pack for manual mode | `mesh/`, `render/`, `pipeline/{tripo_pack,mesh_import,manual_mesh}.py` | built (GLB works fully; FBX needs Blender) |
| Duo checks, Gate 3, export kit (files, checklist, provenance, manifest, secret scan) | `pipeline/{duo,export}.py` | built; export of mock output is blocked |
| Web UI: 15 pages, 9 components, three.js viewer | `duoskin/web` | built, 92 browser tests |
| Windows install: `setup.bat`, `start.bat`, `doctor.bat`, hashed lock for Python 3.12-3.14 | `app/*.bat`, `requirements/` | written and simulated, **never run on Windows** |
| Learning loop: label logging, weekly report, warning auto-hide, regression job over 40 fixed briefs, variety guard, drills, threshold tuner | `engine/calibration.py`, `pipeline/{regression,drills}.py`, Learning and Calibration pages | built, tested in mock mode |

About 58,000 lines of Python, 4,500+ automated tests (unit, API, engine, providers, mesh, clothing, imaging, security, Windows
hygiene, browser).

## 2. Design safeguards against sameness and bad output (all implemented)

- Design card split into WORLD fields (shared) and CHARACTER fields (A and B must differ in at least 2); at most 2 DNA fields and 5
  constraints reach any image prompt (router unit test over every template).
- Pair structure comes from the brief; an open brief mixes structures but never forces it; exactly one plan is always a labelled
  wildcard that ignores the taste profile.
- Menus shown to the planner (hair, eyes, mouths, fabrics, structures, palettes) are shuffled per project with a logged seed;
  structure and palette family rotate as soft hints; "recently used" is a 30-duo sliding window; no copyable examples exist in
  the planner prompts or schemas.
- Taste checks only warn (at most 2 per gate, shown after the first choice, auto-hidden when overridden more than 25%).
  Hard checks are limited to Roblox rules, buildability, IP/logo/known characters, stray text, exact asset reuse, the clone-band
  lower edge, integrity and security.
- Exact reuse of a face or print file is blocked forever; near duplicates inside a 30-duo window.
- Nothing expensive runs before approval; budget cap per duo with an ask threshold per step.

## 3. Reduced modes (the app says so on screen)

- **No head base** (the rigged head that makes blinking faces work in Roblox): faces are made as 2D face layers with a "2D preview"
  badge, and the export has no Head item. The head-base kit contract (APP_SPEC 10.6.1) and `kit_build.py` are **not built**; a
  3D artist or the Blender step still has to produce that kit. Until then the duo ships without custom faces on a rigged head.
- **Empty hair kit**: hair is `hair_custom` (Tripo or manual 3D, then registered onto the head attachment). Kit-based hair works
  once styles are added through the Library page.
- **No Blender**: GLB and glTF import work; FBX import and polish packs need Blender 4.2+.
- **No rapidocr / DreamSim model**: stray-text check falls back to a glyph detector (or reports not-applicable); the clone band
  runs in labelled degraded mode (hash, palette overlap, spec distance).

## 4. Not built or deferred

- Head-base kit and `kit_build.py`; hair polish round trip is optional and basic; H3.1 hair route; AI-made fabric and fold tiles
  (procedural ones ship); extra Gate 3 candidates and face alternatives; storage/GC panel and the test-day runner in the UI.
- OpenAI streaming previews; optional-component downloads in Settings.
- Learning loop details: the variety guard accepts a candidate change only if variety (distribution spread and mean pairwise
  distance) drops by at most 5% and quality does not drop; warnings auto-hide when overridden on more than 25% of the last 20
  showings (at least 8); a check that fires on more than 30% of duos is demoted to warn-only (never Roblox, IP, stray-text,
  security or integrity checks); the threshold tuner needs about 200 labels and only moves bad-tail bounds; drills unlock after 5
  approved duos, 20 items per session, at most 25% of labels. Candidate model arms for the regression job are an in-memory override
  (not a saved setting). `tools/export_dreamsim_onnx.py` has never been run (it needs torch); its pins are UNVERIFIED.
- Anything marked UNVERIFIED in the code (see section 6).

## 5. Deviations from the docs (the code wins)

- Python 3.12 to 3.14 are all supported from one hashed lock (`app/requirements/win-x64.lock`); opencv 5.x instead of 4.x.
- `ProviderError` keeps the spec kinds and adds subclasses; image size drift is `kind="validation", code="size_drift"`.
- Concept images use their own loop in `pipeline/concept.py` (it needs concept ids the shared asset loop cannot carry).
- The "age appropriate" rule reads "covered by clothing or a plain base layer" so crop tops over the required base layer pass.
- Several Roblox numbers are marked `UNV` (unverified) in `checks/thresholds.py` because the creator docs do not state them:
  surface area 70, coplanar 15%, centre offset 1 stud, scale 0.01, shell limit 10, the elbow/knee seam rows (418.5, 467),
  hidden leg rows 355-377, the PNG byte cap.
- The identical-field plan proxy was removed everywhere; consistency checks against the concept, the accessory ceiling (4 or
  more) and the 2+1 structure mix are SOFT.
- Mesh decimation uses an own UV-preserving quadric decimator when pymeshlab is missing.
- The face UV lookup table runs texel-to-canvas (the spec says canvas-to-texel); a converter is not needed until a head base exists.
- One house-style sheet is used for every duo (consistency inside a duo); variety across duos comes from the design card, hair,
  face grammar, outfits, accessories, palette and structure. Multiple rotating house styles would be an easy addition.

## 6. Never tested (do these first, in this order)

1. On a clean Windows 11 PC: `setup.bat`, then `doctor.bat`, then `start.bat` (page opens), save a key, close the window and
   Ctrl+C. Or start the manual **windows** workflow from the repo's Actions tab: it runs setup, doctor and the tests on
   `windows-latest` for Python 3.12 to 3.14 (it never runs on push).
2. Real keys, one cheap probe per provider (Settings > Keys > Test key), then one real duo with a low budget cap. Request shapes
   for Claude and GPT Image were checked against the installed SDK types and the Claude docs; Tripo, Recraft and Gemini shapes
   only against third-party sources, so they are the likeliest to need a tweak on the first real call. Cheapest probes, in
   order of how likely they are to break:
   - Tripo: free file upload, then `image-to-multiview` (about $0.10); check the output URL host.
   - Recraft: free `users/me`, then one vectorize (about $0.01), then one `recraftv4_1_vector` image (about $0.08).
   - Claude: one tiny `claude-opus-5` call (under $0.001) to confirm the fallback option is accepted.
   - OpenAI: Test key (about $0.006), then one low-quality edit with a mask and several images.
   - Gemini (optional): free model lookup, then one judge call (about $0.002).
3. Roblox Studio tests, in order (the helper files are written into the export kit's `studio/` folder when you export): run
   `studio/validation_rules.luau` and compare with `roblox/limits.json`; calibrate the forward axis with `calibration_arrow.gltf`; import one accessory and run **Save to Roblox** validation (do NOT click Submit, that
   pays); the Accessory Fitting Tool comparison; the Block Avatar shirt/pants test (seams 418/467, R6); the head and facial
   animation validation.
4. Quality can only be judged on real output: faces, hair and outfits across 5 to 10 duos. After about 5 approved duos, use the
   calibration screen so thresholds fit your taste.

## 7. Cost and fees (labelled "check Roblox for current prices" everywhere in the app)

AI cost per duo is estimated at about $5-12 (Claude $0.7-1.4, images $1.5-3, Tripo $2-7 for two accessories). Roblox upload
fees and publishing advances are paid only when you upload, by hand, in Studio. There is no Open Cloud upload for Shirt, Pants or
avatar items, so the app exports a kit and a checklist and never uploads.
