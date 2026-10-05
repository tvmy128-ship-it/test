# DuoSkin Studio: application specification

`docs/APP_SPEC.md` · version 1.0 · 2026-09-29 · Status: the build contract for the v1 app. It covers the stack, modules, data model, provider interfaces, job engine, gates, pipeline lanes, UI, HTTP API, settings, install scripts, mock mode, tests and the build order.

DuoSkin Studio is a **local web app for Windows**. A Python backend runs on the user's PC and serves a browser UI at `http://127.0.0.1:<port>/`. The app plans, generates, checks and packages **original Roblox duo skins**: two coordinated blocky characters (boy + boy, girl + girl, boy + girl, girl + boy) that clearly belong together but are never clones. The user makes every part through the app. Nothing comes from the Roblox catalogue. Selling and shops are out of scope.

**Companion documents (all in `docs/`).** Each one owns a different area. This spec points to them instead of repeating them.

| Document | What it owns | How this spec relates to it |
|---|---|---|
| `PROPOSAL_DECISION.md` | The user-approved design decisions: the DNA card (world and character fields), pair structures and their check profiles, at most 2 DNA fields per image prompt, the hard/soft check split, the Gate 1 wildcard, drills as the calibration screen, the replacement of the outline-overlap rule, sliding-window registries, and the variety guard | **Binding.** §3 maps every decision to modules, data and tests. |
| `PROMPT_BIBLE.md` (v1.1) | Every prompt, model parameter, template, slot rule and step ID (L1–L14, I0–I11, R1–R2, T1–T5, G1–G2, C1–C5, H1); the Gate A/Gate B rule IDs; the LLM-facing `DuoSpec` schema; the face grammar; the garment recipes; the cost reference | **Authoritative for anything sent to a model.** Code loads its templates from `duoskin/prompts/`, which holds versioned copies of the bible's templates. |
| `FAILURE_MODES.md` (v1.1) | Every failure mode (`CLO-01`, `ENG-04`, …), the threshold registry (`checks/thresholds.py`), the `CheckResult` model, the must-ship checks (`CHK-*`) and the day-one tests FM-T1…FM-T11 | **Authoritative for checks and thresholds**, except where §2 of this spec applies PROPOSAL_DECISION over it. |
| `DuoSkin_Workflow_Summary.pdf` | The four loops, three gates, checkpoints G0–G6, the fix ladder and the stop rules | Background. |

Test IDs: "FM-T1…FM-T11" are FAILURE_MODES §6 tests. "T1…T5" (without the FM prefix) are the bible's Tripo steps.

---

## 0. Conventions

### 0.1 Markers

| Marker | Meaning |
|---|---|
| **[UNVERIFIED]** | Not confirmed by a primary or local authoritative source. The code must work with either answer; the answer is settled on test day (§19, bible §22, FAILURE_MODES §6). |
| **[DECISION]** | This spec resolves a conflict between sources. See §2. |
| **[CALIBRATE]** | A design threshold. It lives in `checks/thresholds.py` and is tuned on the user's own labels, at about the 5th percentile of approved duos (PROPOSAL_DECISION). |
| **[ESTIMATE]** | A cost or time estimate. The ledger records the real numbers. |
| **[DEPENDS: kit]** | Needs a kit that is built once (head base, hair kit, body base, fabric or fold library). While the kit is missing the app must still work, with its reduced output clearly labelled. |

### 0.2 Source precedence

1. Roblox creator-docs (local copy, commit 2026-09-26); the official Shirt/Pants template PNGs; Roblox's own FBX files; the UGCValidation flag defaults.
2. SDK source code (openai 3.20.0, anthropic 1.9.0) and the bundled Claude API docs.
3. `PROPOSAL_DECISION.md`, the binding design decisions.
4. `PROMPT_BIBLE.md` for model calls; `FAILURE_MODES.md` for checks and thresholds.
5. The fact-checked research reports: GPT Image 2.5, Recraft/Gemini, Tripo v3, Claude roles, Roblox mapping, red team, Windows stack.
6. ComfyUI partner-node source, third-party clients and specs, and web search snippets (always marked).

### 0.3 Terms

| Term | Meaning |
|---|---|
| **Project** | One duo being designed, from the brief to the export. |
| **Spec** | A `DuoSpec` (the planner's JSON, bible §3.2) plus envelope fields that code owns. Every patch creates a new spec version. |
| **DNA card** | A view of the spec: the WORLD fields shared by both characters plus the CHARACTER fields of A and of B (§3.2). It is versioned together with the spec. |
| **Part** | The unit the user approves at Gate 2 and that ships: `a.face`, `a.hair`, `a.acc.0`, `a.print.top.0`, `a.shirt`, `a.pants`, `a.colours`, and the same for `b`. |
| **Asset** | One immutable file in the content store (CAS), addressed by its SHA-256. Drafts, finals, views, meshes, renders and exports are all assets. |
| **Job / Step** | A job is one stage of work (PLAN, PARTS, BUILD, DUO, EXPORT, …). A step is one cached, resumable unit of work inside a job. |
| **Gate / Tile** | A gate is a point where the pipeline waits for the user. A gate holds tiles, and each tile has actions: approve, reimagine, change…, plus per-gate extras. |
| **Gate A / Gate B** | Code checks / yes-no vision-model checks on one asset (bible §7). |
| **G0–G6** | The workflow summary's checkpoints: G0 plan, G1 each image, G2 each asset, G3 files, G4 duo, G5 user pick, G6 Roblox Studio. |
| **HARD / SOFT** | HARD checks may block and enter the fix ladder. SOFT checks only warn or rank (§3.1). |
| **DATA** | The per-user data folder. Default `%LOCALAPPDATA%\DuoSkin\` (§5.3). |
| **EXPORTS** | The user-visible output folder. Default `%USERPROFILE%\DuoSkin Exports\` (§5.3). It holds upload kits, Tripo packs, the inbox and polish packs. |

---

## 1. Scope and requirement traceability

### 1.1 What v1 does

1. **Plan before spending.** The inputs are a brief, a combo and optional references. Claude Opus 5 plans 3 duo specs; when the brief is open they use 3 different pair structures, and exactly one is the labelled wildcard. Then come the code linter, a fresh-context critic with pairwise ranking, a reviser (at most 2 rounds), and cheap concept previews of both characters front and back. Then **Gate 1**.
2. **Part board.** Every part is generated and finalized on its own, checked by code and by a vision model, and shown alone. Then **Gate 2**, with approve / reimagine / change… on every tile.
3. **Build.** This runs only after Gate 2, and only for approved parts:
   - classic Shirt/Pants templates (code compositor);
   - the face: a UV warp onto the head base, or a face-canvas layer pack while no head base exists;
   - hair: kit + fit + human polish, or the Tripo backup, or a manual model;
   - accessories: the Tripo P2 API **or** a manual round trip through Tripo's website, plus sticker slabs and primitives built by code;
   - body colours and the modesty layer.
   
   API meshes and manual meshes go through the same repair and Roblox validation.
4. **Duo loop.** Render both characters and run the duo checks: clone band, anchors, seams, clipping, Roblox validators and the SOFT taste warnings. AI ranking is advisory. Then **Gate 3**, where the user picks, then the **export kit** (files, checklist, `provenance.json`).
5. **Always on:** logo, brand, known-character, stray-text and appropriateness checks. **Only when the user switches it on:** the reference-similarity check.
6. **Calibration and learning:** the calibration labelling screen (drills, once at least 5 approved duos exist), the weekly check report, the 40-brief regression and the variety guard.
7. **Mock mode** for every provider, so the whole app runs without keys.

### 1.2 Not in v1

- Selling, shops and showcase images (PROPOSAL_DECISION #5: later).
- 12 concepts with a critic keeping 3 (PROPOSAL_DECISION #4: later; the revisit triggers are in §3.11).
- Makeup items as a built product (FAILURE_MODES Q5). The planner sets `makeup.kind = none` because the kit manifest says `makeup: unavailable`.
- Uploading to Roblox. There is no upload API for avatar items; the user uploads by hand, following the checklist (§10.13).
- fal. The settings slot and adapter stub exist, but no step calls fal (bible D23).

### 1.3 Requirement traceability

| # | User requirement | Where it is met |
|---|---|---|
| 1 | Complete, coherent 2-character sets for B+B, G+G, B+G, G+B that belong together but are never clones | `Project.combo` (§6.3); the planner and the linter's duo contract (§10.2, bible §9.4); the pair structure and its check profile (§3.3); the clone band (§10.12) |
| 2 | Mostly 2D textures, with 3D accessories only when needed; high-quality clothing textures; no generic AI look, random details or excessive accessories | The clothing compositor (§10.5); accessory build routes (§10.8); restraint lint (accessories: 3 warns, 4 or more fails); house and per-duo style sheets (§10.1, §10.3) |
| 3 | Different faces for both characters and never the same AI face twice; different hair, outfits, accessories, colours and silhouettes with a shared theme; accessories complement each other | Face grammar: A and B differ in at least 3 of 7 fields, plus the sliding-window face registry (§3.7, §10.6); the garment-cut lint; the accessory-complement lint; the A-vs-B silhouette warning |
| 4 | Strong consistency from reference to concept, parts and final assets | Concept of record, palette lock and the per-duo style sheet (§10.3); every part uses its concept crop as a reference; the part-vs-concept check (CHK-A13, CHK-D05); hash-linked approvals (§9.7) |
| 5 | Everything self-made; focus on design and files | No catalogue asset anywhere; kits record their origin (CHK-E09); the export kit (§10.13) |
| 6 | Plan first; approval gates before anything expensive; reimagine or change per part | The plan loop (§10.2); Gates 1–3 (§9); the BUDGET gate (§8.8) |
| 7 | Reference-similarity check only when switched on; logo/brand/known-character checks always on | `ProjectSettings.reference_similarity_check` (default off, §6.3); L13 and the `ip_*` rules always run; L14 and CHK-A15's reference part run only when the check is on (§10.12) |
| 8 | 3D through the Tripo P2 API **or** made by hand on Tripo's website; the app always produces the views and imports the model back | §11: mesh mode `api`, `manual` or `ask`; a Tripo pack on every hair and accessory tile; the import wizard and the inbox watcher |
| 9 | A local web app on Windows; keys: Anthropic, OpenAI and Tripo required, Recraft recommended, Gemini and fal optional | Stack (§4); keyring secrets (§14.2); `setup.bat` and `start.bat` (§15); key routing when a key is missing (§7) |
| 10 | Prompt writing and every detail must prevent wrong or bad output | The prompt compiler and router test (§10.0); fail-closed checks (§3.1); the must-ship check list (Appendix A) |

---

## 2. Contradictions resolved by this spec

The earlier resolutions stay in force: FAILURE_MODES §1 (X1–X28) and bible §1 (D1–D28). The table below covers what those documents leave open or state differently from each other. Each row names the document that must be edited to match.

| # | Topic | Conflict | [DECISION] |
|---|---|---|---|
| S1 | Concept guide geometry | FAILURE_MODES CHK-G1-01 still describes one 2048×1152 sheet with four 512 px slots. The bible (D3, §10.1) makes one call per character at 1536×1024 (two 768×1024 slots) and code assembles a 3072×1024 sheet. | **The bible wins.** It is newer and matches FAILURE_MODES X3 ("two calls by default"). CHK-G1-01 is implemented as: 2 slots of 768×1024 per character call; figure width 480 px at 120 px/stud; mask boxes do not overlap; assembled sheet 3072×1024. *Edit FAILURE_MODES CHK-G1-01.* |
| S2 | `moderation` on OpenAI calls | FAILURE_MODES X2: default `auto`, and send it on edits only if a probe accepts it. Bible D6: omit it everywhere. | **Omit the parameter on every call.** The server default is `auto`, so the effect matches X2 and no probe is needed. |
| S3 | Where the banned-term, phrase, colour-name and style files live | FAILURE_MODES: `kits/banned_terms.json`. Bible: `data/…`. | Shipped, versioned files live in the package: `duoskin/data/{banned_terms,colour_names,phrases,style_guide,rules,checks,prices}.json`. User additions go in `DATA\user_data\banned_terms.extra.json` and are merged at load. |
| S4 | Where kits live | The Windows layout puts `kits\` in the app folder. But the kits (hair styles, head base, body base, fabric and fold libraries, Recraft style ids, house style sheets) belong to the user and grow every month. | **User kits live in `DATA\kits\`**, so an app update never deletes them. Built-in kits defined by code (garment recipes, shoes, legwear, bracelets, the sticker-slab and primitive recipes, the default 2D face canvas) ship in `duoskin/builtin_kits/`. `DATA\kits\manifest.json` is **generated** by merging both (§5.3). |
| S5 | User-visible output folders | Bible §15.2 and the Tripo report put Tripo packs under `<Documents>\DuoSkin\`. FAILURE_MODES X21 and the Windows report use `%USERPROFILE%\DuoSkin Exports\`. | **FAILURE_MODES X21 wins.** Documents and Desktop are Controlled-Folder-Access folders and often redirected to OneDrive. EXPORTS = `%USERPROFILE%\DuoSkin Exports\`, with `Kits\`, `TripoPacks\<duo>\<asset_id>\`, `TripoPacks\inbox\` and `PolishPacks\`. Each path can be changed in Settings. The database and CAS never go there. *Edit bible §15.2 (H1 export folder).* |
| S6 | Mesh export format for Studio | Bible D15: embedded `.gltf` first. FAILURE_MODES X12: `.gltf` + `.bin` + PNG first, `.fbx` with the texture embedded as backup; embedded `.gltf` and `.glb` only after FM-T5. | **FAILURE_MODES X12 wins.** The importer docs list `.fbx`, `.gltf` and `.obj`, and Studio's acceptance of data-URI buffers is unverified. After FM-T5, a capability flag `studio.gltf_embedded_ok` may add the embedded form. *Edit bible D15.* |
| S7 | Pydantic `DuoSpec` | The Windows report had its own schema. | **Bible §3.2 (v1.1) is the LLM-facing schema.** §6.2 mirrors it. A unit test compares the schema hash of `duoskin/models/spec.py` with the hash stored in `duoskin/prompts/SCHEMAS.lock` (written from the bible's version). |
| S8 | A "reject" tile action | The Windows report listed `reject`. | There is no "reject". The tile actions are approve, reimagine and change, plus the per-gate extras (§9.5). |
| S9 | Hair triangle numbers | Bible: Tripo `face_limit` 3500 for `hair_custom`. FAILURE_MODES: hair target 3600. Everywhere: HARD ≤3800. | Tripo `face_limit` 3500 (a target) → repair target ≤3600 (SOFT) → export HARD ≤3800 (the Roblox limit is 4000). |
| S10 | When the 3D build starts | Workflow summary: "only after Gate 2, only for approved parts". But a tile can be approved while others are still open. | **By default, the BUILD job starts when every Gate 2 tile is approved.** The setting `build.start_per_tile` (default off) starts 3D work per approved tile; the UI then warns that a later palette change can force rework. |
| S11 | "Face on the head" when no head base exists yet | The part board must show the face on the head, but the head base is a one-time kit that may not exist yet. | Without a head base, the face tile paints the face canvas onto the front of the blocky mannequin's cube head. It shows **2D expression previews** built from layers (neutral; blink = closed-lid layer; mouth open; happy = raised brows + open mouth), labelled "2D preview — no head base". The Head item is left out of the upload kit, and the face ships as a layer pack (§10.6). |
| S12 | SVG-sanitising library | The Recraft report pins `defusedxml`; the Windows lock does not contain it. | **Add `defusedxml==0.7.1` (pure Python) to `requirements\win-x64.lock`** with its hash. `pyyaml` (already in the lock through omegaconf) is also declared directly, because the prompt loader uses it. |
| S13 | OCR scripts | FAILURE_MODES and the bible ask for Latin + CJK + kana + Hangul. rapidocr 3.9.2 bundles PP-OCRv6 small det/rec models, and which scripts they read is not confirmed for kana and Hangul. | Use the bundled models. Extra recognition models are optional files in `DATA\models\ocr\` [UNVERIFIED availability]. The pseudo-glyph detector (A_GLYPH) and the VLM rule `ip_no_text` cover every script. OCR fails closed if it cannot run. |
| S14 | Tripo input size | Bible T1: 2048² input. But the I4/I5 finals are 1024 (or 1024×1536). | Before T1, code upscales the final 2× with Lanczos and re-pads it to the canonical framing (object 80–85% of the long side). Tripo's own output views are never transformed (ACC-04). |
| S15 | Claude streaming entry point | Windows report: `client.messages.stream`. Claude report and bible: `client.beta.messages.stream` with `fallbacks="default"`. | **FAILURE_MODES X27.** Opus routes use `client.beta.messages.stream(betas=["server-side-fallback-2026-07-01"], fallbacks="default")`. The local docs confirm: the `"default"` form uses the `-2026-07-01` header, the array form uses `-2026-06-01`, and mixing them is a 400. Sonnet routes use `client.messages.stream` until the startup check finds a non-empty `allowed_fallback_models`. |
| S16 | The body's modesty layer | Policy: a lower layer for every skin-like body; an upper layer when the chest is rounded or the character resembles a minor. | **Both layers on every character** (conservative, because blocky figures can read as young). They sit under the Shirt and Pants, so they cost nothing visually. |
| S17 | Tripo key "required" versus manual mode | Requirement 9 lists Tripo as required; manual mode must still work. | The Tripo key is required for the **full** workflow, because T1 multiview (10 credits) is the default source of side and back views even in manual mode (FAILURE_MODES X15). Without a key, side views come from I10 (GPT), flagged lower-reliability on the tile, and 3D is manual only. |
| S18 | How "the brief names a structure" is known | PROPOSAL_DECISION: the brief wins, and rotation applies only when the brief is open. FAILURE_MODES PLN-09 requires the planner to echo `brief_constraints[]`, but the bible's `PlanSet` has no such field. | Two parts. (1) The brief form has a **"Pair structure"** dropdown: `let the planner choose` (default) or one named structure. A named structure is a hard lint (all 3 specs use it). With `let the planner choose`, the specs must use either 3 different structures (open brief) or the same structure 3 times (the planner read one into the brief; Gate 1 shows "Your brief was read as *same club*"). A mix of 2 + 1 is a HARD lint fail that goes to the reviser. (2) **Must-include lines** (up to 5, each ≤12 words) are typed on the brief form, and the bible gains `PlanSet.brief_constraints: list[BriefConstraint{text, spec_paths}]`. Each must-include line must map to a path that resolves in every spec. Until the bible adds the field, must-include coverage is checked by the critic (`taste_fit` evidence) and listed on the Gate 1 card. *Edit bible §3.2 (PlanSet) and §9.3.* |
| S19 | Hard or soft: colour distances in the duo contract | FAILURE_MODES CHK-G0-06 (structure colour rules) and CHK-D03 (colour anchor on both fronts) are HARD. The bible's C1 lists main-colour ΔE and anchor ΔE as SOFT. PROPOSAL_DECISION: "Hard checks cover only Roblox rules, buildability, IP/logos and the clone band"; main-colour and anchor distances are soft. | **SOFT** (binding decision). Main-colour ΔE (per structure profile), anchor-colour ΔE ≤6 and colour-anchor visibility at Gate 3 warn and rank only. The **structural** duo-contract rules stay HARD at plan lint, because PROPOSAL_DECISION itself says the linter "rejects" them and they are the plan-level clone-band proxy: 2–3 anchors visible from the front, ≥5 measurable contrasts on distinct axes (colour axes ≤2), faces differing in ≥3 of 7 fields, hair kit A ≠ B, the garment-cut rule, and ≥2 differing CHARACTER DNA fields. *Edit FAILURE_MODES CHK-G0-06 and CHK-D03 to SOFT.* |
| S20 | Concept palette adherence | FAILURE_MODES CHK-G1-04: own colours within ΔE ≤12 is HARD. Bible §10.1: A_SWATCH is a warning only, because the palette is re-extracted from the approved picture. | **Split.** Partner-colour leakage (A_LEAK, >3% of the figure) stays HARD. Adherence to the planned colours is SOFT (PROPOSAL_DECISION: "You approved a picture, not text"). *Edit FAILURE_MODES CHK-G1-04.* |
| S21 | Invalidating "both characters" after a change | FAILURE_MODES ENG-01 says to invalidate downstream for both characters, because duo checks depend on both. Forcing re-approval of the partner's unchanged parts would make every change redo the whole board. | An approval is invalid when anything that **changes the part's own output** changes (§9.7). The partner's parts are re-**checked** on the pair-dependent rules (A_LEAK, the face A-vs-B difference, the garment cut, hair A ≠ B); they keep their approval if those still pass. The Gate 3 duo candidate always becomes STALE. |
| S22 | Where the "≤6 colours" rule lives | The old print prompt carried "≤6 colours". PROPOSAL_DECISION moves it to palette snap. | Palette snap (A_PALETTE and the compositor) enforces colour counts. No colour counts in any prompt (router test, U9). |
| S23 | Library assets while the libraries are still empty | The bible builds fabric (I7) and shading (I8) libraries once, "per duo only if missing"; fold sets need human curation. | A missing fabric tile is built on demand by I7 as a LIBRARY step, with its own BUDGET line. A missing fold set falls back to `builtin_kits/folds_procedural`: code-drawn soft bands from the recipe geometry, labelled "procedural folds" on the tile. |
| S24 | Is "consistency with the approved picture" a taste check? | PROPOSAL_DECISION's HARD list does not name it. FAILURE_MODES makes finalize drift (A_DRIFT, CHK-A10) and part-vs-concept palette (CHK-A13, CHK-D05, ΔE ≤12) HARD. | **HARD, as an integrity check** (policy class `consistency`): the user must receive what they approved (requirement 4, ENG-01), and these checks compare built against approved, never against a taste target. The looser "same design?" DreamSim/VLM comparisons stay SOFT. |

---

## 3. Implementing `PROPOSAL_DECISION.md`

PROPOSAL_DECISION is binding. This section says where each decision lives in code, which data it uses, and which test proves it. The adoption order in §18 follows its "Adoption order".

### 3.1 Hard vs soft check policy (safeguards)

**Rule.** HARD checks cover only these classes:
- Roblox rules and validators;
- buildability from the kits;
- IP, logo and known-character checks;
- stray text;
- exact reuse of a registered face or print file (forever), and near-duplicates within the sliding window;
- the clone band's lower edge;
- file integrity and security invariants.

Everything else is SOFT: all of upgrade 3, main-colour and anchor distances, restraint colour and motif counts, novelty, and every taste measure.

**Where it lives.**
- `duoskin/data/checks.json` lists every check ID with its `kind` (`hard | soft | assert`), its `policy_class` (`roblox | buildability | ip | stray_text | registry | clone_lower_edge | integrity | security | taste | colour_distance | restraint | novelty | consistency`), the stage, and the warning text shown to the user.
- `duoskin/checks/policy.py` loads the file and exposes `meta(check_id) -> CheckMeta`.

```python
# duoskin/checks/policy.py
HARD_CLASSES = {"roblox", "buildability", "ip", "stray_text", "registry", "clone_lower_edge", "integrity", "security",
                "consistency"}   # consistency = part-vs-concept palette (CHK-A13/D05) and finalize drift; FM marks them HARD
class CheckMeta(Strict):
    check_id: str; kind: Literal["hard", "soft", "assert"]; policy_class: str
    stage: Literal["startup", "call", "G0", "gate1", "gate2", "build", "gate3", "export", "always"]
    threshold_keys: list[str]           # names in checks/thresholds.py
    warn_text: str                      # plain sentence shown under "approve anyway?"
    demotable: bool                     # may be auto-demoted to warning when miscalibrated (DES/UNV thresholds only)
```

**Enforcement.**

| Safeguard | Implementation | Test |
|---|---|---|
| Soft checks never use a fix from the 3-fix cap, never climb above rung 1, never trigger a regenerate or a plan revision, and never change an approved part | `engine/ladder.py::next_action()` sees only HARD/ASSERT failures. SOFT results go to `Part.open_warnings` and to ranking scores. The rung-1 auto-fix may still run for a soft failure, but only on unapproved candidates and without counting as a fix. | `test_ladder_soft_never_climbs`: an asset with only soft failures ends at `Done` with warnings, and `fixes_used == 0` |
| A soft check can never be marked HARD | `test_policy_classes`: every `taste`, `colour_distance`, `restraint` or `novelty` check has `kind == "soft"` | unit |
| At most 2 warnings per gate, shown **after** the user's first choice ("approve anyway?") | `api/gates.py` holds the warnings back until the first decision on that gate arrives. The response then carries at most 2 (chosen by the highest catch rate on rejected duos, then severity), and the UI shows them as a confirm step (§9.9) | API test |
| Every override is logged as a label | `labels` row `kind="warning_override"`, `source="gate"` | API test |
| A warning overridden >25% of the time (over its last 20 showings) hides itself until it is re-tuned | `engine/calibration.py::warning_visibility()` | unit |
| Weekly report per check: how often it flags duos the user approved, and how often it catches duos the user rejected | `engine/calibration.py::weekly_report()` → the Learning page (§12) | unit on fixture labels |
| All HARD checks together may reject at most about 10% of approved duos; any check firing on >25–30% of duos goes back to warning-only | The report raises an alert. A `demotable` check whose fire rate is above `calib.demote_fire_rate` (0.30) is switched to SOFT in `settings.check_overrides` and a banner says so. Checks in the classes `roblox`, `ip`, `stray_text`, `security` and `integrity` are never demotable. | unit |
| Thresholds cut only the bad tail and never set a target | Threshold tuning (§3.8) sets each [DES] value at about the 5th percentile of approved duos. No check has a "target" value; `thresholds.py` has no ratios or counts that describe a desired look | review checklist + unit test on the tuner |
| Checks fail closed | `checks/runner.py::run_check()` wraps every check; an exception or a missing dependency gives `ran=False, passed=False` (FAILURE_MODES §0.3 rule 4) | unit |

### 3.2 Upgrade #1: the DNA card

| Decision | Implementation |
|---|---|
| Split the card into WORLD fields shared by both characters and CHARACTER fields for A and B | `DuoSpec.world` (`WorldDNA`: theme, pair_structure, structure_note, story, palette_family, material_family, detail_level) plus `shared_anchors` = WORLD. `Character.dna` (`CharacterDNA`: shape_language, colour_plan, focal_location, motif_object, accessory_style, energy) plus `hair.kit_style_id` = CHARACTER. Line weight, light and shading bands come from the house-style references, not from the card. `models/dna.py::card_from_spec(spec) -> DnaCard` builds the view (§6.4). |
| A and B differ in at least 2 CHARACTER fields, and these count toward the 5+ contrasts | Lint rule `PLN-DNA-01` (HARD, class `buildability`/clone proxy): count the differences in {shape_language, colour_plan, focal_location, hair.kit_style_id, motif_object (normalised text), accessory_style (normalised text)} and require ≥2. The contrasts that match (`shape_language` and so on) count toward the 5. |
| Fixed values where code routes or compares; free text for flavour | Enums: pair_structure, palette_family, material_family, detail_level, colour_plan, focal_location, shape_language, anchor kind, hair kit id. Free text (word-capped, linted): theme, motif_object, accessory_style, energy, story. |
| Each image call gets its fixed rules plus at most 2 DNA fields, 5 constraints or fewer in total; a character's fields go only to that character's calls | `prompts/dna_router.py` holds the routing table (bible §3.3). `route_dna(template_id, spec, character) -> list[DnaSlot]` returns at most 2 fields, all from `character`. **Router unit test** `tests/unit/test_prompt_router.py` (§17.2): every template × 3 real fixture specs × both characters. |
| Palette hexes, palette ratio, colour count, story and pair structure never go into an image prompt | The compiler has no slot source for these fields, and `lint_prompt()` rejects hex codes, the story text (substring match) and the pair-structure words. Router test. |
| Order of locking: the planner drafts the card; after Gate 1, code extracts the palette from the approved front and back concept and overrides the card's colour fields | C3 (`concept.lock`, §10.3): per-zone k-means in CIELAB → snap-or-confirm (CHK-G1-08). The palette hexes are replaced, the palette IDs stay stable, `palette_source="concept_extracted"`, and the card becomes version 1 with `locked=True`. |
| "Lock" means a versioned default, not a wall | A Gate 2 or Gate 3 "Change…" becomes an L7 patch → new `SpecRecord` (version + 1) and a new `DnaCard` version with a field diff. `engine/deps.py` finds the parts that use each changed field (through the routing table and the dependency rules, §9.8), and only those tiles are redone. Fix-ladder rung 5 (revise the plan) may also rewrite the card, but only after a CHANGE_CONFIRM gate. |
| Log every card; the planner sees the last 5 as "recently used, prefer something else if the brief allows" (a hint, not a lint) | Table `dna_cards` (§6.13). `pipeline/plan.py` renders the last 5 cards as compact JSON into `<recent_cards>`. No lint rule reads this table. |

### 3.3 Upgrade #2: pair structure, check profiles, anchor rotation

| Decision | Implementation |
|---|---|
| The brief wins | §2 S18: the brief form's "Pair structure" dropdown and the planner instruction for implied structures. Lint `PLN-STR-01` (HARD). |
| When the brief is open, the 3 plans use different structures (a planner instruction, not quota code); "other" is allowed | Planner role text (bible §9.3). The lint accepts 3 different structures or 3 identical ones; `other` requires `structure_note`. No quota code. |
| Log the structure picks of the first ~10 duos; build least-recently-used weighting only if the log shows collapse (one structure in >35% of duos) | `structure_log` = a view over approved specs. The Learning page shows the shares. Setting `planner.structure_lru_hint` (default **off**) adds "least used recently: …" to `<recently_used>`. The page suggests turning it on when one structure passes 35% after ≥10 duos. |
| Each structure selects a check profile | `duoskin/data/structure_profiles.json`, loaded by `pipeline/lint.py` (table below). |
| The linter rejects a plan whose contrasts are mostly colour swaps | HARD: colour axes (`colour_temperature`, `value`) count as at most 2 of the contrasts, and at most 1 under `same_club`. |
| `object_mascot` means two human blocky characters, with the mascot as the signature accessory or the linked accessory pair | Lint: `presentation ∈ {boy, girl}` for both (always true by schema); at least one accessory with `kind ∈ {plush_pet, keychain_charm, prop}` and `linked_to_partner=True`, or an anchor of kind `accessory_pair`. |
| The story is metadata only | The story is never a slot source (router test). It is shown on the Gate 1 card and stored in the DNA card log. |
| The critic's 2 new questions are 1–5 ranking inputs, never pass/fail | L4 criteria `structure_readable` and `accessory_pair_expresses` feed only the ranking score (bible §9.5). `plan.select` never drops a plan because of them. |
| Remove the single worked example from the planner prompt | The planner role text has no example (bible §9.3). Test `test_planner_prompt_has_no_example`: the compiled L3 system prompt contains no JSON object with a `palette` key and none of the retired example words ("night market", "lantern gold", "hyper", "deadpan"). |
| Rotate the anchor type; the ΔE ≤6 anchor check applies only to colour anchors | The planner is asked to vary anchor kinds, and `recently_used.anchor_kinds` is a hint. The anchor-colour check runs only when `anchor.kind == "colour"` (SOFT, §2 S19). |

**Structure check profiles** (`data/structure_profiles.json`; every colour row is SOFT per §2 S19; the contrast and cut rows are HARD):

| Structure | Main-colour rule (SOFT) | Contrast rule (HARD) | Garment-cut rule (HARD) | Typical anchor kinds (hint) |
|---|---|---|---|---|
| `complement` | `a_main` vs `b_main` ΔE2000 ≥15 | ≥5 measurable, colour axes ≤2 | different top **or** bottom type, and ≥2 differing cut attributes | colour, motif, material |
| `leader_chaotic` | ΔE ≥15 | as complement, and `expression` or `shape_language` among the contrasts | as complement | motif, accessory_pair |
| `same_club` | none (may share a main colour) | ≥5 measurable, colour axes ≤1; ≥4 from hair, face, cut, print or accessory axes | same garment type allowed, but ≥2 differing cut attributes | material, trim, motif |
| `mirror` | swapped roles: ΔE(`a_main`, `b_second`) ≤10 and ΔE(`b_main`, `a_second`) ≤10 | as complement, plus different hair kit style, garment type and accessory category | different top or bottom type | colour, silhouette_detail |
| `seasonal_twins` | different `palette_family` season group; main ΔE ≥15 | ≥4 non-colour contrasts | as complement | motif, material |
| `object_mascot` | as complement | as complement | as complement | accessory_pair |
| `other` | none | as complement | as complement | any |

### 3.4 Upgrade #3: taste checks as warnings only

All of these are SOFT forever. None of them ever blocks, ever uses a fix, or ever changes an approved part. They run where they are cheapest.

| Check | Where it runs | How it is computed | Shown as |
|---|---|---|---|
| **Phone-size top colours** (KEEP) | Gate 3 (duo renders); preview at Gate 2 from the flat tiles | Area-downscale the front beauty render to 120–150 px tall and mode-downscale the label map to match. Run k-means in CIELAB over clothes and hair pixels only (skin excluded; masks eroded by 1 px; shading bands merged by the label map). Warn if the planned main colour is not in the top 2. The old "main ΔE ≥15" check is folded in here and applies only under structures that want different mains (§3.3). | warning DUO-03 |
| **A-vs-B hair and accessory silhouette** (KEEP; a difference measure only, never "bigger is better") | Plan lint: precomputed kit-hair IoU matrix (front + side). Gate 2: the 2D front-view alpha minus the known head-guide mask. Build: the ID pass | IoU of A's and B's hair+accessory masks. Warn above the 95th percentile of approved duos [CALIBRATE]. Also warn when an accessory is smaller than `taste.acc_min_px` at phone size. | warnings PLN-15, DUO-04 |
| **Declared vs built colour plan** (OPTIONAL, low priority) | Build (compositor label maps) | Each character's `colour_plan` names a ratio (60/30/10, 70/20/10, 50/50 block, mono + accent, all-over pattern). Count clothes-only pixels on the composited FRONT+BACK template areas at 64 px/stud (skin excluded; bracelets and shoes count as accent). Warn if a share is off by more than 15 points. No universal ratio. Setting `taste.ratio_check` (default on); drop it if it never catches anything (the weekly report shows its catch rate). | warning |
| **Detail level vs detail count** | Plan lint | Range tied to `detail_level`: minimal 1–2, standard 2–3, maximal 4–5 details (prints + trims + accessories), warning only. | warning |
| AI focal-point yes/no | — | **DROPPED.** The DNA's `focal_location` is a planner hint only. | — |
| Fixed "3 fashion signals" | — | **DROPPED** (replaced by the detail-level range above). | — |
| Fixed 60/30/10 | — | **DROPPED** (replaced by the declared-vs-built check above). | — |

The ID/label pass (§10.11) supplies exact masks: a three.js or numpy render with no anti-aliasing, `NearestFilter`, no mipmaps, no tone mapping, and one flat colour per part (skin, modesty, head, hair, each accessory, each sticker). The compositor writes a label map next to each 585×559 template (fabric, secondary block, print, trim, bracelet, shoes, legwear, transparent/skin). No AI segmentation is used anywhere.

### 3.5 The Gate 1 wildcard (in place of upgrade #4)

- The planner returns exactly 3 specs. When the brief is open they differ in structure, palette family or anchor kind (HARD, CHK-G0-07), and **exactly one has `is_wildcard=True`**.
- The wildcard ignores the taste profile; the critic receives "wildcard: ignore taste_fit". It must still pass buildability, the Roblox rules and the plan-level clone proxies. Soft taste preferences never rank it down.
- It gets its own cheap preview (I1 drafts, 4 Flare-low per character, ≈$0.02–0.03) and is shown at Gate 1 as a **labelled alternative** ("Wildcard").
- `plan.select` guarantees that the 3 shown plans include the wildcard, even when the critic ranks it last.
- **Tracking.** The Learning page shows how often the user picks the wildcard. When the pick rate over the last 10 duos is ≥30%, the page suggests raising `taste_profile.soft_weights.novelty_tiebreak`. The user confirms; nothing changes automatically.

### 3.6 Replacing the whole-body outline-overlap rule

- **Removed:** no check named `outline_overlap` exists. Test `test_no_outline_overlap_rule` (PLN-07).
- **(a) Garment-cut lint, HARD at plan time** (`pipeline/lint.py`, PLN-05): A and B differ in top **or** bottom recipe family, **and** in at least 2 of {sleeve, hem, leg, neckline, front, block_layout}. Under `same_club` the family may be the same, but the 2-attribute rule still holds (§3.3).
- **(b) Garment layout similarity, SOFT at build** (`checks/taste.py::garment_layout_similarity`): compare A's and B's compositor label maps front and back. Measure garment-vs-skin coverage per region, and colour-block layout similarity **ignoring the actual colours**, as the adjusted Rand index between the two label partitions over the FRONT+BACK areas. Warn above the calibrated band [CALIBRATE].
- **(c) A-vs-B hair and accessory silhouette overlap**, SOFT (§3.4).
- **DreamSim stays the overall clone check** (HARD lower edge, CHK-D02; degraded mode until an ONNX model exists, §10.12).

### 3.7 Registries, cross-duo memory and kit-reuse hints

| Decision | Implementation |
|---|---|
| Face and print registries use a sliding window: near-duplicates are blocked against the last ~30 duos or the items currently listed; exact-file reuse stays blocked forever | `imaging/similarity.py::registry_check(kind, candidate) -> CheckResult`. Exact: the SHA-256 of the decoded RGBA pixels is compared against **all** registry rows (HARD forever). Near-duplicate: pHash ≤8 and DreamSim <0.15 [CALIBRATE] against rows whose `duo_seq > current − face.registry_window_duos` (30), plus rows flagged `listed=True` (HARD). |
| Track the lint reject rate; a rising rate means grow the kits | The weekly report plots the registry and plan-lint reject rates per 10 duos. Above 15% the Library page shows "grow the kits: eye shapes, mouths, hair styles". |
| Soft kit-reuse hint; no hard caps | `recently_used` (a derived view over the last 10 duos: hair kit ids, eye shapes, mouth styles, palette families, fabric ids, pair structures, anchor kinds) goes into the planner's `<recently_used>` as a hint. There is no cap, quota or field-combination lint (test `test_no_novelty_lints`). |
| Cross-duo memory after Gate 3 | `duo_memory` row: the 4-side render DreamSim embedding (or the degraded pHash vector), the DNA card and the kit ids. The nearest past duo gives a SOFT warning when "very close" [CALIBRATE] and a tie-break in the Gate 3 ranking (weight `novelty_tiebreak`). |
| Answer repeats by growing the kits, not by stricter rules | The Library page has a "Kit growth plan" panel listing the most-reused kit ids and a target of a few new pieces per month. |

### 3.8 Upgrade #6: drills as the calibration labelling screen

- **When.** The Calibration page unlocks drills once **at least 5 approved duos** exist (`labels` needs varied bases).
- **Variants** (code only, $0 in API; `pipeline/drills.py`):
  - recolours (the colourways code: palette permutations and hue shifts within the palette family);
  - hair kit swaps between A and B, or to the nearest kit style;
  - accessory swaps;
  - print swaps;
  - face-variant swaps (grammar fields exchanged between A and B);
  - a few **obvious clones** (B = A recoloured) and **obvious strangers** (A from one duo + B from another) as anchors.
  
  Renders reuse the approved meshes and templates (§10.11).
- **Session rules.** At least 5 base duos covering several combos and colour-plan types. At most about 5 variants per base per session. Sessions of 10–15 minutes (a timer and a "stop here" button).
- **Blind labelling.** No check verdicts are shown before the label. The labels are `clone | real_duo | strangers`, plus `like | dislike`.
- **Label sources.** Real gate decisions and warning overrides are labels too (`source="gate"`). Drill labels are at most ~25% of the calibration set: `engine/calibration.py` down-weights drill labels when they exceed 25%.
- **Use.** Labels set only **bad-tail bounds** (the worst acceptable value) at about the 5th percentile of approved duos: the clone band's lower edge, the main-colour and anchor ΔE bands, and the silhouette warnings. They never set target values.
- **Isolation.** Drill images never enter the taste profile, the registries or the critic examples. Assets are tagged `stream="drill"`, and the loaders assert that no `drill` asset is read (test `test_drill_isolation`).
- **Target.** About 200 labels (FM-T10) before the [DES] thresholds are re-tuned. The 40-brief set is used only for before/after regression, never for picking thresholds.

### 3.9 Learning loop and variety guard

- **Weekly learning loop** (Learning page, §12):
  - top failure causes by FAILURE_MODES ID;
  - the per-check flag and catch rates (§3.1);
  - structure use, nearest-duo distances, Gate 1 first-try approval and the wildcard pick rate;
  - the registry reject rate;
  - cost per duo.
- **Regression job** (`JobKind.REGRESSION`, `python -m duoskin regression`): runs the fixed **40-brief set** (`DATA\regression\briefs.json`, user-editable, frozen per version) through the plan loop and Gate 1 previews. It uses mock gates: the top-ranked plan is auto-approved for measurement only, and nothing is exported.
- **Variety guard.** On the SAME 40 briefs, before and after a change (template version, model snapshot, threshold set, kit manifest, house style), compute:
  - variety = mean pairwise DreamSim of the 40 concept sheets (degraded: pHash);
  - the counts of distinct pair structures, hair kit styles and palette families.
  
  A change that improves the pass rate or rating but lowers variety by more than 5% is **rejected**: its version is not promoted to the project default.
- **Promotion.** Settings → Versions shows "candidate" versions next to the "default" ones. Promotion requires a passing regression plus the variety guard. Projects pin their versions when they leave BRIEF (ENG-08).

### 3.10 Other workflow fixes

| Fix | Implementation |
|---|---|
| Soften the restraint rule: "no stray text" stays HARD; "at most 4 main colours" and "too many motifs" become plan-lint warnings that a plan may override with `detail_level=maximal` or `colour_plan=allover_pattern` | `pipeline/lint.py` restraint rules are SOFT (§3.1). The accessory ceiling stays: 3 is SOFT, **≥4 is HARD** (the user requirement "no excessive accessories"; FAILURE_MODES X16/Q7). |
| Move checks to the earliest cheap stage | Hair pairing at plan lint (kit IoU matrix); colours and silhouettes of 2D parts at Gate 2, before Tripo spend and hair polish; the garment-cut rule at plan lint. |
| Move "≤6 colours" and anything else code enforces out of prompts | Palette snap, the transparency API parameter plus the alpha check, and framing by guide. The router test rejects colour counts in prompts (§2 S22). |
| Tune thresholds from ~200 labels plus real gate overrides, not from the 40-brief set | §3.8. |

### 3.11 Deferred upgrades and their revisit triggers

- **#4 (12 concepts).** Not built. After about 10 real duos the Learning page evaluates the triggers and shows a notice if either fires:
  - nearest-duo distances shrink over the last 10 duos;
  - one structure or palette family exceeds 35%;
  - Gate 1 first-try approval is below 50% [CALIBRATE].
- **#5 (showcase).** Not built. When it is built later: AI paints the background only, code composites the real renders, and showcase images and sales data never enter the registries, the taste profile, the critic examples or the Gate 3 ranking.

---

## 4. Stack (pinned) and Windows notes

### 4.1 Runtime

| Layer | Pin | Notes |
|---|---|---|
| Python | **CPython 3.14.x x64** (target; 3.14.7 current). **CPython 3.13.x x64** is the supported fallback (3.13.15). Both install from **one** hashed lock. | 3.11 is impossible (numpy 2.5.3, scipy 1.18.1 and PyWavelets 1.10.0 need ≥3.12). 3.12 has had no Windows installers since 3.12.10. 3.13's last Windows binary is due in October 2026 (PEP 719). Refuse free-threaded `3.14t`, 32-bit and ARM64 builds. On Windows on ARM, install **x64** Python (pymeshlab has no win_arm64 wheel; performance under emulation is [UNVERIFIED]). |
| Web | fastapi 0.141.1, starlette 1.7.0, uvicorn 0.54.0 (**no** `[standard]` extra), python-multipart 0.0.32, sse-starlette 3.5.0 | Sync `def` routes for DB work run in the thread pool. No GZip middleware, because it buffers SSE. `docs_url=None` unless dev mode (Swagger UI loads from a CDN). |
| Models | pydantic 2.13.5 (pydantic-core 2.46.5) | One model set for the API, DB JSON and LLM structured outputs. |
| DB | stdlib `sqlite3` (SQLite 3.50.4 bundled with CPython 3.13/3.14) | WAL, `RETURNING`, JSON functions and STRICT tables are available. No ORM. |
| HTTP / SDKs | anthropic 1.9.0, openai 3.20.0, **httpx2 2.13.1** (module `httpx2`, httpx-compatible), truststore 0.10.4 | Both SDKs are built on httpx2, which checks TLS against the Windows certificate store. Our Tripo and Recraft REST clients use httpx2 too. `truststore.inject_into_ssl()` at startup makes `requests` (a rapidocr dependency) and httpx (google-genai) use the Windows store as well. |
| Secrets, paths | keyring 25.7.0 (+pywin32-ctypes 0.2.3), platformdirs 4.12.1 (`appauthor=False`) | Windows Credential Manager, with a DPAPI-file fallback (§14.2). |
| Imaging | pillow 12.3.0, numpy 2.5.3, opencv-python 4.14.0.94 (not headless; stay on 4.x), scikit-image 0.26.0, scipy 1.18.1, imagehash 4.3.2 (+PyWavelets 1.10.0) | All file I/O goes through Pillow, or `np.fromfile` + `cv2.imdecode` (unicode paths). |
| SVG | resvg-py 0.5.0 (bundles resvg 0.48.1), **defusedxml 0.7.1 (added, §2 S12)** | No Cairo DLL. The sanitizer rules are in §10.4.2. |
| OCR | rapidocr 3.9.2 + onnxruntime 1.30.0 + omegaconf 2.3.1 + antlr4-python3-runtime 4.9.3 **wheel from `wheelhouse\`** | A binary-only install without the wheel resolves omegaconf 2.0.6 and rapidocr crashes. The lock carries the wheel hash `b6e01ff2…fef3a`. |
| Similarity | imagehash (pHash/dHash); DreamSim as an ONNX model in `DATA\models\dreamsim.onnx`, run on onnxruntime [UNVERIFIED] | No torch. Without the model file the clone band runs in labelled **degraded** mode (FAILURE_MODES X19, Q1). |
| Mesh | trimesh 5.1.0, manifold3d 3.5.4, networkx 3.6.1, pymeshlab 2025.7.post1, fast-simplification 0.2.0 (untextured meshes only), pygltflib 1.16.5 | Only pymeshlab's `meshing_decimation_quadric_edge_collapse_with_texture` keeps UVs. trimesh cannot read FBX or meshopt. pymeshlab is imported **only** in the mesh-worker subprocess (it rewrites `PATH` and `QT_PLUGIN_PATH`). |
| Process | psutil 7.2.2 | Kills subprocess trees; sweeps orphans. |
| Optional (`requirements\optional.lock`) | google-genai 2.25.0 (`<3.0.0`), DracoPy, xatlas 0.0.11 (no cp314), ufbx 0.0.5 (no cp314), assimp-py 1.2.0 | Installed from Settings → "Optional components". |
| Dev (`requirements\dev.lock`) | pytest 9.1.1, ruff 0.16.9 | Mock HTTP with `httpx2.MockTransport`. respx and pytest-httpx work only after `httpx2.alias_httpx()` is loaded as an early pytest plugin. |
| Frontend | three 0.186.1 vendored (`build/three.module.js` + `build/three.core.js`; the addons GLTFLoader, OrbitControls, RoomEnvironment and FBXLoader with the files they import), vanilla ES modules, `// @ts-check` + JSDoc | No bundler, no CDN, one import map. |
| External, optional | Blender 5.2 LTS (minimum 4.2; `bpy.ops.wm.fbx_import` needs ≥4.5, the legacy `import_scene.fbx` is the fallback on 4.2–4.4) | FBX import/export, multi-material bakes, polish packs and one-time kit builds. The app works without Blender (§10.7, §11). |
| External, the user's | Roblox Studio | Free tests and the uploads are done by hand (§10.13). |

The resolved install is about 600 MB [ESTIMATE]. The first install under Defender real-time scanning takes several minutes.

### 4.2 Lock files and wheelhouse

```
requirements\win-x64.lock    73 pins + defusedxml 0.7.1 (added), --generate-hashes, cp313 AND cp314 wheel hashes;
                             antlr4 lists its wheel hash (the wheel comes from wheelhouse\)
requirements\optional.lock   google-genai, DracoPy, xatlas, ufbx, assimp-py
requirements\dev.lock        pytest, ruff
wheelhouse\antlr4_python3_runtime-4.9.3-py3-none-any.whl
tools\make_lock.ps1          uv pip compile --python-platform x86_64-pc-windows-msvc --only-binary :all:
                             --find-links wheelhouse --generate-hashes  (run for 3.14 and 3.13; the pins must match)
tools\check_lock.py          verifies every pin has a cp313 and a cp314 win_amd64 (or py3-none-any) hash (CI)
```

Source for the lock: `scratchpad/requirements-win-py313.lock` (73 packages), renamed to `win-x64.lock` because the same file installs on cp313 and cp314 (FAILURE_MODES X22).

### 4.3 Windows notes that shape the code

| Pitfall | Rule in code | FM ID |
|---|---|---|
| `.js` is served as `text/plain` because of a registry MIME entry, and ES modules refuse to load | `mimetypes.add_type` for `.js .mjs .css .svg .webp .glb .gltf .wasm` before mounting static files | SYS-13 |
| OpenCV fails on non-ASCII paths | All I/O goes through `imaging/files.py` (Pillow or `np.fromfile` + `cv2.imdecode`) | SYS-05 |
| ANSI code page (cp1252) | `PYTHONUTF8=1` in the .bat files; `encoding="utf-8"` on every `open()` and `subprocess.run` (ruff rule PLW1514 in CI) | SYS-06 |
| The Store stub or the wrong interpreter flavour | The .bat files use `py -V:3.14`, then `py -V:3.13`, then `.venv\Scripts\python.exe`. `doctor` requires `win-amd64`, a GIL build, a venv interpreter, and `sys.base_prefix` not under `WindowsApps`. | SYS-10 |
| OneDrive, Controlled Folder Access and antivirus locks | DB and CAS live in `%LOCALAPPDATA%`; exports in `%USERPROFILE%\DuoSkin Exports\`; writes go to a temp file, then `os.replace` with retries for up to 2 s; `doctor` runs a write test and warns about OneDrive paths | SYS-14 |
| MAX_PATH (260 characters) | Install to `C:\DuoSkin\app`; CAS paths are `cas\ab\<64hex>.<ext>`; a path linter requires `len < 240`, a slug regex, and no reserved names (CON, NUL, …) | SYS-15 |
| Port ranges reserved by Hyper-V/WSL/Docker; uvicorn `sys.exit`s on a bind failure | Bind the socket ourselves with `SO_EXCLUSIVEADDRUSE`: the sticky port, then 8765, then 8766–8799. WinError 10013 and 10048 mean "try the next port". Pass `sockets=[sock]` to uvicorn. | SYS-12 |
| Clicking inside the console (QuickEdit) freezes output | Turn QuickEdit off at startup (`SetConsoleMode` via ctypes) | SYS-16 |
| Closing the console window ends the process after about 5 s without cleanup [reasoned] | `SetConsoleCtrlHandler` for CLOSE/LOGOFF/SHUTDOWN does a fast shutdown; leases and WAL make a hard kill safe; the UI has a Quit button | SYS-16 |
| Ctrl+C hangs on pool joins or open SSE streams | Shutdown: set cancel flags → `executor.shutdown(wait=False, cancel_futures=True)` → WAL checkpoint → `os._exit(0)` after a 2-second grace period; `timeout_graceful_shutdown=2`; the WinError 10054 log line is filtered | SYS-16 |
| The laptop sleeps during a long job | `SetThreadExecutionState(ES_CONTINUOUS \| ES_SYSTEM_REQUIRED)` from the long-lived scheduler thread (the flag is per thread) while the queue is not empty | SYS-16 |
| CPU oversubscription | Set `OMP_NUM_THREADS=1` and `OPENBLAS_NUM_THREADS=1` in `__main__` before importing numpy; `cv2.setNumThreads(1)` in pool threads; one `RapidOCR` instance behind a lock | SYS-17 |
| onnxruntime "DLL load failed" (needs msvcp140 and msvcp140_1, which it does not bundle) | At startup, `os.add_dll_directory(sys.prefix)` and `sys.prefix\Scripts` when `msvcp140.dll` is there (installed by `msvc-runtime`). `doctor` checks `ctypes.WinDLL("msvcp140.dll")` and `("msvcp140_1.dll")` in a subprocess and links the VC++ 2015–2022 x64 redistributable. OCR fails closed. | SYS-09 |
| Windows N/KN editions: `import cv2` fails (MF.dll missing) | `doctor` imports cv2 in a subprocess and links the Media Feature Pack on failure | SYS-21 |
| `blender-launcher.exe` returns at once, and Blender exits 0 even when the script fails | Call `blender.exe` with `--background --factory-startup --disable-autoexec --python-exit-code 3`; results come back through a JSON file, never stdout; `CREATE_NO_WINDOW`; psutil tree kill on timeout | SYS-11 |
| A downloaded zip is blocked by SmartScreen | README: open the zip's Properties and tick **Unblock** before extracting | SYS-19 |
| `localhost` resolves to `::1` while the server listens on IPv4 | Always use `127.0.0.1` in URLs | SYS-19 |
| A module named like a stdlib module shadows it (a stray `inspect.py` broke numpy and anthropic imports during the research, and did so again while this spec was written) | Package modules are never named `secrets`, `io`, `inspect`, `types`, …; always launch with `-m duoskin` from the app root; CHK-S13 compares names with `sys.stdlib_module_names` | SYS-19 |
| Cookies are not port-scoped; a cached `index.html` holds an old token | The token goes in a `<meta>` tag of an `index.html` served `no-store`; no auth cookie | SYS-02, ENG-11 |

---

## 5. Architecture and project layout

### 5.1 Process model

```
 Browser tab (the leader tab holds the only EventSource; other tabs relay through BroadcastChannel)
   │  HTTP fetch with X-DuoSkin-Token             ▲ SSE /api/events (id = event row id)
   ▼                                              │
 uvicorn (one process; bound to 127.0.0.1:<sticky port> through a socket we bind ourselves)
   ├─ FastAPI app: routers /api/*, static /web and /vendor, CAS files at /cas/<sha>.<ext>
   ├─ EventBus: writes the events table first, then call_soon_threadsafe to the subscribers
   ├─ Scheduler thread ─► claims READY steps (lease) ─► pools:
   │     api  : 6 threads; per-provider semaphores: anthropic 3, openai 3 (+ images-per-minute bucket),
   │            recraft 2 (+ 25 calls/min at n=4), tripo 2 (image-generation category 1 slot; paid P2 submits one at a
   │            time), gemini 2
   │     cpu  : min(4, cpu_count-1) threads (numpy / OpenCV / Pillow / resvg / numpy rasteriser)
   │     proc : 1 slot for subprocesses: python -m duoskin.mesh.worker job.json, Blender
   ├─ Heartbeat thread: extends the leases of RUNNING steps every 30 s
   ├─ Inbox watcher thread: polls EXPORTS\TripoPacks\inbox\ and EXPORTS\PolishPacks\*\return\ every 2 s
   └─ SQLite (WAL) + CAS on disk under DATA
```

- Remote tasks (Tripo) never hold a thread. `run()` submits and commits `remote_ref`; `poll()` is scheduled with backoff.
- Heavy native modules (onnxruntime/rapidocr, cv2, scipy, trimesh) are imported lazily, so `/api/health` answers within about 1 s even while Defender scans DLLs.
- Mesh repair, pymeshlab, FBX parsing and Blender always run in a subprocess with a timeout. A native crash cannot kill the server, and pymeshlab's Qt DLLs stay out of the main process.

### 5.2 Code layout (package `duoskin`)

```
C:\DuoSkin\app\                          (not Downloads, not OneDrive)
  setup.bat  start.bat  doctor.bat  README.txt  pyproject.toml  .gitattributes (*.bat text eol=crlf)
  requirements\  wheelhouse\  tools\  tests\
  duoskin\
    __init__.py  __main__.py   CLI: run | doctor | reset-leases | export-diagnostics | gc | regression
                               | build-kit-manifest | calibrate-report
    app.py                     create_app(): mimetypes fix, middleware, routers, static mounts
    config.py                  Settings model (§14), paths (platformdirs, appauthor=False), atomic settings.json IO
    keystore.py                get_key / set_key / delete_key / key_status: keyring -> DPAPI file -> env (dev)
    security.py                TrustedHost, Origin + X-DuoSkin-Token check, CSP, no-store index.html, CAS sandbox headers
    logsetup.py                JSON log lines, RedactFilter (regexes + the exact stored key values), faulthandler, excepthooks
    winplat.py                 QuickEdit off, SetThreadExecutionState, SetConsoleCtrlHandler, SO_EXCLUSIVEADDRUSE bind,
                               single-instance lock (msvcrt.locking), add_dll_directory, DPAPI (ctypes)
    data\                      banned_terms.json colour_names.json phrases.json style_guide.json rules.json checks.json
                               structure_profiles.json prices.json template_regions.json (copy of roblox\...) skin_tones.json
    builtin_kits\              recipes\*.json shoes.json legwear.json bracelets.json primitives.json slab.json
                               face_canvas_default.json folds_procedural.json mannequin_blocky.json
    db\                        schema\001_init.sql …  db.py (conn(), tx(), migrations with PRAGMA user_version)  repo.py
    models\                    common.py spec.py (LLM-facing; mirrors bible §3.2) dna.py project.py spec_record.py part.py
                               asset.py job.py gate.py cost.py settings.py registry.py kitenums.py llm_io.py (role schemas)
    checks\                    model.py (CheckResult, gate_verdict)  thresholds.py (FAILURE_MODES §4.1)  policy.py (§3.1)
                               runner.py (fail-closed wrapper)  gate_a.py (A_* ids)  gate_b.py (RuleId enum, L11 calls)
                               taste.py (§3.4 warnings)  plan_rules.py (C1 rule functions)
    engine\                    scheduler.py registry.py context.py cache.py cas.py bus.py budget.py recovery.py heartbeat.py
                               gc.py deps.py (dependency graph + invalidation) ladder.py (fix + technique ladders)
                               calibration.py (labels, warning stats, weekly report, threshold tuner)
    providers\                 base.py (errors, CallCtx, RateLimiter, capability flags)  anthropic_llm.py  openai_images.py
                               recraft.py  tripo.py  gemini.py  fal.py (stub)  registry.py (real | mock | disabled)
                               pricing.py  mock\{llm,images,recraft,tripo,gemini}.py  fixtures\ (mock data)
    prompts\                   <ID>.md templates with YAML front matter (copies of the bible)  SCHEMAS.lock  registry.py
                               compiler.py (fill slots, lint = CHK-P01, hash)  dna_router.py  system_blocks.py
    pipeline\                  plan.py concept.py parts.py assetloop.py face.py hair.py accessory.py clothing.py
                               colours.py build.py duo.py export.py manual_mesh.py polish.py lint.py (C1) change.py (L7)
                               drills.py regression.py library.py (I7/I8 library builds, S0 bootstrap)
    roblox\                    template_regions.json (from analysis/classic_template_r15_map.json)  template.py
                               limits.json (boxes per type + attachment + scale; budgets; docs commit)  validators.py
                               checklist.py (item-type table)  luau.py (accessory wrapper + property checker snippets)
    imaging\                   files.py masks.py guides.py svg.py checks.py ocr.py glyph.py similarity.py palette.py
                               matte.py (sentinel unmix) compositor.py face_canvas.py uvwarp.py slab_art.py colournames.py
    mesh\                      load.py repair.py validate.py worker.py blender.py slab.py primitives.py orient.py export.py
                               blender_scripts\ (fbx_import.py fbx_export.py bake_atlas.py polish_pack.py kit_build.py)
    render\                    raster.py (numpy z-buffer: beauty + ID passes)  avatar.py (dress the mannequin)  sheets.py
    api\                       health.py state.py events.py settings.py keys.py doctor.py projects.py specs.py gates.py
                               parts.py assets.py uploads.py imports.py jobs.py costs.py exports.py library.py
                               calibration.py learning.py os_open.py focus.py shutdown.py
    web\                       index.html app.js api.js events.js router.js styles.css
                               views\  (home setup settings brief plan gate1 board build gate3 export jobs library
                                        calibration learning costs)
                               components\ (tile.js viewer3d.js brushmask.js warnings.js costbar.js stagebar.js
                                            dropzone.js dnacard.js diff.js)
                               vendor\three\0.186.1\{build,examples\jsm}\
  tests\  _alias_httpx.py  unit\  golden\  property\  api\  providers\  e2e_mock\  fixtures\
  tools\  vendor_three.py  make_lock.ps1  check_lock.py  build_portable.py
```

### 5.3 Data folders

```
DATA = %LOCALAPPDATA%\DuoSkin\        (platformdirs.user_data_dir("DuoSkin", appauthor=False);
                                       override: DUOSKIN_HOME, or portable.flag next to the app -> .\data\)
  settings.json            Settings (§14), atomic write
  duoskin.sqlite3 (+ -wal, -shm)
  secrets.dpapi            only when keyring fails (DPAPI-encrypted; §14.2)
  cas\ab\<sha256>.<ext>    every asset, immutable
  kits\                    user kits  [DEPENDS: kit]
    manifest.json          GENERATED: builtin_kits + user kits, sorted, each entry with sha256, origin, license
    hair\<style_id>\       style.json (prompt_phrase, length class, symmetric, silhouette class), mesh.glb,
                           albedo_grey.png, bands.png, views\{front,left,back,right}.png, sil.npz
    hair\modules\<id>\     fringe and back modules (same layout)
    hair\pair_iou.json     precomputed front/side silhouette IoU between kit styles (plan lint, PLN-15)
    head_base\<variant>\   head.fbx, poses\{neutral,blink,jaw_drop,happy,sad}.glb, face_canvas.json, uv_lut.npz,
                           zones.json, stretch.npz, head_mesh.sha256, studio_validated.json
    body_base\             body.fbx (15 MeshParts, *_Geo), modesty_masks.png, body_uv.json, studio_validated.json
    fabrics\<id>\          tile.png (1024 greyscale seamless), fabric.json (material_family, prompt_phrase, px_per_repeat)
    folds\<set>\<panel>.png  multiply/screen overlays per recipe panel, fold.json
    style\                 house_style_v<N>.png, recraft_styles.json (vector and raster style_id registries, kept apart)
  models\                  dreamsim.onnx (optional), ocr\ (optional extra models), matting\ (optional)
  regression\              briefs.json (the 40 briefs), results\<version>.json
  user_data\               taste_profile.json, banned_terms.extra.json
  logs\  run\ (instance.lock, server.json)  tmp\  backups\ (5 rotating DB backups)

EXPORTS = %USERPROFILE%\DuoSkin Exports\    (each path can be changed in Settings; FAILURE_MODES X21)
  Kits\<duo_slug>_<yyyymmdd-hhmm>\           upload kits (§10.13)
  TripoPacks\<duo_slug>\<asset_id>\          manual 3D packs (§11.3)
  TripoPacks\inbox\                          drop GLB/FBX/ZIP files here (or onto the tile)
  PolishPacks\<duo_slug>\<part_id>\          hair polish round trip (§10.7); the user saves into ...\return\
  Diagnostics\                               redacted diagnostic zips
```

`kits\manifest.json` is rebuilt by `python -m duoskin build-kit-manifest`, and at startup when any kit folder's mtime changed. The loader refuses any kit asset without `origin ∈ {user_made, code_generated, app_generated, roblox_reference}` and a sha256 (POL-08). A new `kit_manifest_sha` rebuilds the kit `Literal` enums (`models/kitenums.py`), re-runs the Claude schema smoke test (CHK-S10) and re-renders the `KIT_INVENTORY` system block.

**Kit availability flags** in the manifest drive routing: `hair_kit_empty`, `head_base_present`, `body_base_present`, `makeup: unavailable` (v1), `fabric_ids[]`, `fold_sets[]`, `recraft_face_style_id`.

### 5.4 Module list: responsibilities and interfaces

| Module | Responsibility | Key interface |
|---|---|---|
| `config` | Settings and paths | `load_settings() -> Settings`; `save_settings(s: Settings) -> None`; `paths() -> Paths` |
| `keystore` | API keys | `get_key(provider) -> str \| None`; `set_key(provider, value) -> None` (rejects >1280 chars); `delete_key(provider)`; `key_status() -> dict[Provider, KeyStatus]` (never returns the value; masked `sk-…abcd` only) |
| `security` | Local-only hardening | `install(app, token: str, importmap_sha: str) -> None` |
| `winplat` | Windows integration | `bind_socket(preferred: int) -> socket.socket`; `keep_awake(on: bool)`; `console_ctrl_handler(cb)`; `quickedit_off()`; `single_instance() -> InstanceLock \| None`; `dpapi_protect(b) / dpapi_unprotect(b)` |
| `db` | Connections, transactions, migrations | `conn() -> sqlite3.Connection` (per thread); `tx() -> ContextManager` (`BEGIN IMMEDIATE`); `migrate()` |
| `db.repo` | Typed CRUD | `get_project(id) -> Project`; `save_part(p, expected_version)` (409 on conflict); … one pair per model |
| `engine.cas` | Content store | `put(data: bytes, ext: str, *, link: AssetLink, prov: Provenance) -> Asset`; `path(sha) -> Path`; `get(sha) -> bytes`; `pixel_sha(png) -> str` |
| `engine.cache` | Step cache | `cache_key(step, handler, params, inputs) -> str`; `lookup(key) -> CachedResult \| None`; `store(key, result)` |
| `engine.scheduler` | Claim, dispatch, retry, backoff, pause | `start()`; `stop()`; `submit_job(kind, project_id, params) -> Job`; `spawn(job_id, steps: list[Step])`; `pause(project_id)`; `resume(project_id)` |
| `engine.deps` | Dependency graph and invalidation | `affected_parts(old: DuoSpec, new: DuoSpec, redo: list[Redo]) -> InvalidationReport`; `dna_field_users(field, character) -> list[PartId]` |
| `engine.ladder` | Fix and technique ladders per part | `next_action(part, outcome, budget) -> LadderAction` |
| `engine.budget` | Estimates, reservations, BUDGET gate | `reserve(project_id, est: Estimate) -> Reservation`; `commit(res, actual: CostEntry)`; `release(res)`; `remaining(project_id) -> float` |
| `engine.bus` | Events | `emit(type, payload, project_id=None) -> int` |
| `engine.calibration` | Labels, warning statistics, weekly report, threshold tuning | `log_label(...)`; `warning_visibility(check_id) -> bool`; `weekly_report() -> Report`; `tune(threshold_key) -> Proposal` |
| `checks.runner` | Run any check, fail-closed | `run_check(check_id, subject_sha, fn, **facts) -> CheckResult` |
| `checks.policy` | Hard/soft classes | `meta(check_id) -> CheckMeta`; `is_blocking(result) -> bool` |
| `prompts.compiler` | Template → prompt text; lint; hash | `compile(template_id, spec, character, slots, ctx) -> CompiledPrompt`; `lint_prompt(cp) -> list[CheckResult]` |
| `prompts.dna_router` | DNA routing (≤2 fields, own character) | `route_dna(template_id, spec, character) -> list[DnaSlot]` |
| `providers.*` | External APIs and mocks | §7 |
| `pipeline.*` | Step handlers per stage and lane | §8.2, §10 |
| `roblox.template` | Regions, crops, gap fill, adjacency | `REGIONS: dict[str, Box]`; `crop(img, region)`; `fill_gaps(img, owner_map)`; `ADJACENCY` |
| `roblox.validators` | Template and mesh rules | `validate_template(png, kind, label_map, recipe) -> list[CheckResult]`; `validate_accessory(mesh_facts, asset_type, attachment, scale="Classic") -> list[CheckResult]` |
| `imaging.*` | 2D I/O, guides, masks, checks, SVG, compositor, face canvas, UV warp | §10 |
| `mesh.*` | Import, repair, validate, slabs, primitives, Blender bridge | §10.9 |
| `render.*` | Deterministic server-side renders (beauty + ID passes), contact sheets | §10.11 |
| `api.*` | HTTP routes | §13 |
| `web` | UI | §12 |

---

## 6. Data model

### 6.1 Conventions

- **Two kinds of models.**
  - *LLM-facing* models (`models/spec.py`, `models/llm_io.py`) follow bible §2.8 and §3.1:
    - no `Optional`, no unions, no defaults, no `dict`, no recursion;
    - lowercase snake_case enums, lowercased by a `BeforeValidator`;
    - every field has a `Field(description=…)`.
    
    Counts, word caps and hex patterns are enforced by Pydantic `model_validator`s (`models/spec_rules.py`) and the linter, never by the API grammar.
  - *Internal* models (everything else) may use `Optional`, `dict` and defaults.
- Every model subclasses `Strict` (`extra="forbid"`). JSON stored in SQLite is `model_dump_json()` output. Canonical hashing uses `canonical_json(obj)`: `json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)` encoded as UTF-8.
- IDs are ULIDs with a type prefix (`prj_…`, `spc_…`, `job_…`, `stp_…`, `gat_…`, `dec_…`, `chg_…`, `cst_…`, `lbl_…`). Events use the integer row id. Part IDs are semantic (§6.6).
- Times are UTC `datetime` with a timezone; the UI shows local time.
- Money is `float` USD, stored rounded to 1e-6. Tripo credits are stored as well (`credits`).

```python
# duoskin/models/common.py
from __future__ import annotations
import json, hashlib
from datetime import datetime
from enum import StrEnum
from typing import Annotated, Any, Literal
from pydantic import BaseModel, ConfigDict, Field

class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")

Sha256 = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
Slug   = Annotated[str, Field(pattern=r"^[a-z0-9][a-z0-9_-]{0,40}$")]
PartId = Annotated[str, Field(pattern=r"^(duo|(a|b)\.(face|hair|shirt|pants|colours|acc\.[0-9]|print\.(top|bottom|shoes|charm)\.[0-9]))$")]
CharKey = Literal["a", "b"]
Provider = Literal["anthropic", "openai", "recraft", "tripo", "gemini", "fal"]

def canonical_json(obj: Any) -> bytes:
    if isinstance(obj, BaseModel):
        obj = obj.model_dump(mode="json")
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")

def sha256_of(obj: Any) -> str:
    return hashlib.sha256(obj if isinstance(obj, bytes) else canonical_json(obj)).hexdigest()
```

### 6.2 `DuoSpec` (LLM-facing; mirrors bible §3.2 v1.1)

The planner returns a `PlanSet` through structured outputs. **The bible is the authority** for field meanings and descriptions; this is the code shape. Kit enums (`HairKit`, `FringeKit`, `BackKit`, `TopRecipeKit`, `InnerTopKit`, `BottomRecipeKit`, `FabricKit`, `ShoeKit`, `SkinToneKit`, `EyeShapeKit`, `MouthKit`) are built at startup from `kits/manifest.json` by `models/kitenums.py`. They are sorted and include the sentinels the bible lists (`hair_custom`, `kit_default`, `none`). If the schema smoke test reports "Schema is too complex", the largest kit enum becomes `str` and the linter rejects unknown IDs (bible §3.1).

```python
# duoskin/models/spec.py  (LLM-facing). Field(description=…) omitted here for brevity; the code copies every
# description verbatim from bible §3.2, because descriptions are part of the prompt.
from typing import Annotated, Literal
from pydantic import BeforeValidator, Field
from .common import Strict
from .kitenums import K          # K.HairKit, K.FringeKit, … (Literal types built from the manifest)

def E(*values: str):
    return Annotated[Literal[values], BeforeValidator(lambda v: v.strip().lower() if isinstance(v, str) else v)]

Combo         = E("bb", "gg", "bg", "gb")
PairStructure = E("complement", "leader_chaotic", "same_club", "mirror", "seasonal_twins", "object_mascot", "other")
PaletteFamily = E("warm_pastel", "cool_pastel", "warm_bright", "cool_bright", "earthy_natural", "muted_vintage",
                  "jewel_tones", "candy_bright", "neon_night", "monochrome_accent")
DetailLevel   = E("minimal", "standard", "maximal")
MaterialFamily= E("jersey", "twill", "denim", "fleece", "nylon", "knit", "canvas", "corduroy")
ShapeLanguage = E("round_soft", "sharp_angular", "boxy_sturdy", "flowing_curved", "spiky_energetic", "geometric_clean")
ColourPlan    = E("ratio_60_30_10", "ratio_70_20_10", "block_50_50", "mono_accent", "allover_pattern")
FocalLocation = E("chest", "back_print", "hair", "accessory", "shoes", "face")
Region = E("torso_u", "torso_f", "torso_b", "torso_l", "torso_r", "torso_d",
           "rlimb_u", "rlimb_f", "rlimb_b", "rlimb_l", "rlimb_r", "rlimb_d",
           "llimb_u", "llimb_f", "llimb_b", "llimb_l", "llimb_r", "llimb_d")

class Colour(Strict):
    id: str; name: str; hex: str
    role: E("a_main", "a_second", "b_main", "b_second", "shared", "accent", "neutral_light", "neutral_dark",
            "hair_a", "hair_b", "modesty", "line")

class WorldDNA(Strict):
    theme: str; pair_structure: PairStructure; structure_note: str; story: str
    palette_family: PaletteFamily; material_family: MaterialFamily; detail_level: DetailLevel

class Anchor(Strict):
    kind: E("colour", "motif", "material", "trim", "silhouette_detail", "accessory_pair", "hair_detail", "face_detail")
    description: str; on_a: str; on_b: str; visible_from: E("front", "both")

class Contrast(Strict):
    axis: E("colour_temperature", "value", "hair_shape", "hair_length", "top_type", "bottom_type", "sleeve_length",
            "leg_length", "neckline", "layering", "block_layout", "pattern_scale", "fabric", "accessory_kind",
            "accessory_slot", "face_eyes", "face_mouth", "expression", "shape_language")
    a_value: str; b_value: str

class CharacterDNA(Strict):
    shape_language: ShapeLanguage; colour_plan: ColourPlan; focal_location: FocalLocation
    motif_object: str; accessory_style: str; energy: str

class Body(Strict):
    skin_tone: K.SkinToneKit; modesty_ref: str

class Face(Strict):
    eye_shape: K.EyeShapeKit
    iris_style: E("oval_solid", "oval_top_band", "oval_two_step", "oval_ring", "round_small_pupil", "vertical_slit")
    highlight_style: E("dual_dot", "single_large", "sparkle_star", "triple_dot", "crescent_rim", "none_matte")
    lash_style: E("clean_line", "outer_flick_1", "outer_flicks_3", "wing", "heavy_line_lower_ticks")
    brow_style: E("thin_arched", "straight_thick", "short_round", "angled_up", "soft_worried")
    mouth_style: K.MouthKit
    nose_style: E("none", "dot", "tiny_hook", "shadow_tick")
    cheek_mark: E("none", "blush_soft", "blush_hatch")
    default_expression: E("neutral", "soft_smile", "smug", "sleepy", "determined", "cheerful")
    iris_ref: str; iris_dark_ref: str; pupil_ref: str; sclera_ref: str; lash_ref: str; brow_ref: str
    mouth_line_ref: str; mouth_inner_ref: str; tongue_ref: str; teeth_ref: str; blush_ref: str

class Hair(Strict):
    kit_style_id: K.HairKit; fringe_id: K.FringeKit; back_id: K.BackKit
    description: str; colour_ref: str; shadow_ref: str; highlight_ref: str

class Print(Strict):
    motif: str; region: Region; scale: E("small", "medium", "large"); colour_refs: list[str]

class Top(Strict):
    recipe_id: K.TopRecipeKit
    sleeve: E("none", "short", "three_quarter", "long"); hem: E("crop", "waist_tucked", "hip_untucked")
    neckline: E("crew", "v_neck", "collar", "hood", "high_zip", "square"); front: E("closed", "open", "layered")
    block_layout: E("solid", "contrast_sleeves", "raglan_split", "horizontal_band", "vertical_split", "yoke")
    inner_recipe_id: K.InnerTopKit; fabric_id: K.FabricKit
    base_ref: str; second_ref: str; trim_ref: str
    prints: list[Print]
    arm_extras: list[E("bracelet_char_right", "bracelet_char_left", "gloves", "wristband_char_right", "wristband_char_left")]

class Shoes(Strict):
    style_id: K.ShoeKit; base_ref: str; sole_ref: str; accent_ref: str; motif: str

class Bottom(Strict):
    recipe_id: K.BottomRecipeKit
    leg: E("mini", "above_knee", "knee", "midi", "full"); waist: E("low", "mid", "high")
    fabric_id: K.FabricKit; base_ref: str; second_ref: str; trim_ref: str
    prints: list[Print]
    legwear: E("bare", "socks_ankle", "socks_crew", "socks_knee", "tights"); legwear_ref: str
    shoes: Shoes

class Accessory(Strict):
    kind: E("plush_pet", "keychain_charm", "bag", "small_hat", "hair_clip_slab", "sticker_slab", "prop")
    description: str
    category: E("hat", "hair", "face", "neck", "shoulder", "front", "back", "waist")
    attachment: E("hat", "hair", "face_front", "face_center", "neck", "right_shoulder", "left_shoulder",
                  "right_collar", "left_collar", "body_front", "body_back", "waist_front", "waist_center", "waist_back")
    size_class: E("small", "medium", "large")
    build: E("tripo", "sticker_slab", "code_primitive")
    material: E("plush", "vinyl", "rubber", "knit", "canvas", "enamel_flat", "wood")
    linked_to_partner: bool; colour_refs: list[str]

class Makeup(Strict):
    kind: E("none", "freckles", "beauty_mark", "cheek_heart", "cheek_star", "eyeshadow",
            "multicolour_lips", "multicolour_lashes", "face_paint")
    description: str; colour_refs: list[str]

class Character(Strict):
    presentation: E("boy", "girl"); role_in_duo: str
    dna: CharacterDNA; body: Body; face: Face; hair: Hair; top: Top; bottom: Bottom
    accessories: list[Accessory]; makeup: Makeup

class DuoSpec(Strict):
    combo: Combo; lead: E("a", "b"); is_wildcard: bool
    world: WorldDNA; shared_anchors: list[Anchor]; contrasts: list[Contrast]; palette: list[Colour]
    a: Character; b: Character

class BriefConstraint(Strict):                 # pending bible edit (§2 S18); behind flag plan.brief_constraints_field
    text: str; spec_paths: list[str]

class PlanSet(Strict):
    specs: list[DuoSpec]; how_they_differ: str
    # brief_constraints: list[BriefConstraint]   (added when the bible adopts §2 S18)
```

Pydantic validators (`models/spec_rules.py`) enforce what the grammar cannot: word caps, `^#[0-9A-Fa-f]{6}$`, unique palette IDs, every `*_ref` resolving (or `none` where allowed), 2–3 anchors, ≥5 contrasts, 5–12 palette entries, 1–4 colour refs, prints per garment. The role I/O schemas for L1, L2, L4–L7 and L9–L14 (`ReferenceAnalysis`, `TasteProfile`, `Critique`, `PairJudgment`, `Revision`/`PatchOp`, `ChangePlan`/`ImageFix`/`Redo`, `HairMatch`, `RepairPlan`, `AssetCheck`/`Verdict`, `DuoJudgment`/`DuoReview`, `IpCheck`, `SimCheck`) live in `models/llm_io.py`, copied from the bible sections of the same names. `prompts/SCHEMAS.lock` stores the schema hash of each class; a unit test fails when code and lock differ (§2 S7).

### 6.3 `Project`

```python
# duoskin/models/project.py
class Stage(StrEnum):
    BRIEF = "brief"; PLANNING = "planning"; GATE1 = "gate1"; PARTS = "parts"; GATE2 = "gate2"
    BUILDING = "building"; DUO = "duo"; GATE3 = "gate3"; EXPORTING = "exporting"; EXPORTED = "exported"

class ReferenceImage(Strict):
    asset_sha: Sha256
    role: Literal["reference", "favourite"]        # reference = this duo; favourite = a taste source (setup)
    note: str = Field(default="", max_length=200)

class ProjectSettings(Strict):                      # per-project copies of the global defaults (§14)
    budget_usd: float = 15.0                        # hard cap per duo (thresholds: budget.per_duo_usd)
    ask_above_usd: float = 2.0                      # per-step "ask me" threshold -> BUDGET gate
    reference_similarity_check: bool = False        # requirement 7: only when switched on (L14, CHK-A15 reference part)
    use_reference_as_mood: bool = False             # bible D13: off by default; turning it on recommends L14 in a banner
    mesh_mode: Literal["api", "manual", "ask"] = "ask"
    hair_route: Literal["auto", "kit", "tripo_api", "manual"] = "auto"
    gemini_second_opinion: bool = False
    concept_quality: Literal["low", "medium"] = "low"
    build_start_per_tile: bool = False              # §2 S10

class VersionPins(Strict):                          # frozen when the project leaves BRIEF (ENG-08)
    models: dict[str, str]                          # role -> snapshot, e.g. {"image_draft": "gpt-image-2.5-flare-2026-09-08"}
    prompt_versions: dict[str, int]                 # template id -> version
    schema_hashes: dict[str, str]
    house_style_version: int; style_guide_version: int
    kit_manifest_sha: Sha256; thresholds_version: str; rules_version: int
    roblox_docs_commit: str = "2026-09-26"
    app_version: str

class Project(Strict):
    id: str; name: str = Field(max_length=60); slug: Slug
    created_at: datetime; updated_at: datetime
    combo: Literal["bb", "gg", "bg", "gb"]          # first letter = character a
    brief: str = Field(max_length=2000)             # raw user text: data, never instructions
    structure_request: str = "auto"                 # "auto" or a PairStructure value (§2 S18)
    must_include: list[str] = Field(default_factory=list, max_length=5)   # each <= 12 words, linted
    references: list[ReferenceImage] = Field(default_factory=list, max_length=4)
    stage: Stage; paused: bool = False
    plan_job_id: str | None = None
    approved_spec_id: str | None = None             # set by the Gate 1 approval; advanced by every applied patch
    current_spec_id: str | None = None
    duo_seq: int | None = None                      # position in the approved-duo sequence (registry window)
    settings: ProjectSettings
    pins: VersionPins | None = None
    spent_usd: float = 0.0                          # denormalised from the ledger
    version: int = 0                                # optimistic lock
```

### 6.4 `SpecRecord`, patches and `DnaCard`

```python
# duoskin/models/spec_record.py
class PatchOp(Strict):                              # LLM-facing (L6, L7): the value is JSON text
    op: E("replace", "add", "remove"); path: str; value_json: str; reason: str

class SpecRecord(Strict):
    id: str; project_id: str
    plan_set_id: str; plan_index: int               # 0..2 inside its PlanSet
    parent_spec_id: str | None
    version: int                                    # 1 = planner output; +1 per applied patch
    created_by: Literal["planner", "reviser", "change", "palette_lock", "user"]
    patch_from_parent: list[PatchOp] = []
    spec: DuoSpec
    schema_version: Literal[1] = 1
    text_policy: Literal["no_text"] = "no_text"     # code constant, never a model field
    palette_source: Literal["planner", "concept_extracted"] = "planner"
    status: Literal["candidate", "dropped", "shown", "approved", "superseded"]
    lint: list[CheckResult] = []
    critic_levels: dict[str, str] = {}              # criterion -> level (L4)
    pairwise_wins: float = 0.0                      # L5, both orders; disagreement = 0.5 each
    rank: int | None = None
    sha256: Sha256                                  # sha256_of(spec)

# duoskin/models/dna.py
class DnaCard(Strict):                              # a VIEW of the spec (PROPOSAL_DECISION #1), versioned with it
    spec_id: str; version: int                      # = SpecRecord.version at the time
    locked: bool                                    # True from C3 on (a versioned default, not a wall)
    source: Literal["planner", "concept_extracted", "change", "revise_plan"]
    world: WorldDNA
    anchors: list[Anchor]
    palette_family: str; palette_hexes: dict[str, str]   # id -> hex (never sent to an image model)
    a: CharacterDNA; b: CharacterDNA
    hair_kit: dict[CharKey, str]
    diff_from_previous: list[str] = []              # changed field paths

def card_from_spec(rec: SpecRecord, locked: bool, source: str) -> DnaCard: ...
def character_field_differences(card: DnaCard) -> list[str]: ...   # for lint PLN-DNA-01 (>= 2 required)
```

**Applying a patch** (`pipeline/change.py::apply_patch`):
1. Parse each `value_json` with `json.loads`.
2. Reject an op whose path is not allowed:
   - reviser (L6): only paths named by a finding, or their children;
   - change interpreter (L7): any path except `/combo`, `/is_wildcard`, `/a/presentation`, `/b/presentation`, and palette **ids** (hexes may change).
3. Apply RFC 6902.
4. Re-validate with Pydantic, then re-lint (C1).
5. Store a new `SpecRecord` with `version + 1` and a new `DnaCard` version with `diff_from_previous`.

A validation failure or a HARD lint failure rejects the patch and shows the reason; the old spec stays current.

### 6.5 Part kinds

A part is the unit of approval (Gate 2) and of shipping. `pipeline/parts.py::plan_parts(spec) -> list[Part]` creates parts from the approved spec.

| Part id | Kind | Created when | Gate 2 tile shows | Build output |
|---|---|---|---|---|
| `c.colours` | colours | always | skin tone, modesty colour, palette swatches (main, second, shared, accent, hair), body front/back flat preview | BodyColors + modesty texture (body kit) |
| `c.face` | face | always | the face on the head: 4 expressions × 5 skin tones; the top 2–3 assembled faces as alternatives | head texture (head base) or a face layer pack |
| `c.hair` | hair | always | front view + 4 matching views; the kit match score when a kit exists | hair mesh (kit fit + polish, Tripo, or manual) |
| `c.acc.<i>` | accessory | one per accessory | `tripo`: front + 4 views + on-body scale; `sticker_slab`: badge art + slab preview + scale; `code_primitive`: primitive preview + scale | mesh `.gltf`+`.bin`+PNG, `.fbx`, fit notes, Luau snippet |
| `c.print.<slot>.<i>` | print | one per print, shoe motif or bracelet charm | the graphic alone plus a 100 px readability preview | used by the compositor |
| `c.shirt` | shirt | always | flat front and back (code), 3D box preview | `shirt.png` 585×559 |
| `c.pants` | pants | always | flat front and back with shoes and legwear (code), 3D box preview | `pants.png` 585×559 |
| `duo` | duo | after BUILD | (Gate 3) duo sheet, phone strip, face poses | the export kit |

### 6.6 `Part`

```python
# duoskin/models/part.py
class PartKind(StrEnum):
    COLOURS = "colours"; FACE = "face"; HAIR = "hair"; ACCESSORY = "accessory"; PRINT = "print"
    SHIRT = "shirt"; PANTS = "pants"; DUO = "duo"

class PartState(StrEnum):
    PLANNED = "planned"               # created; nothing generated yet
    GENERATING = "generating"         # asset loop running
    READY = "ready"                   # finals passed HARD checks; the tile waits for the user
    NEEDS_HUMAN = "needs_human"       # ladder rung 6: best-so-far shown with a report
    APPROVED = "approved"             # ApprovalRecord valid
    STALE = "stale"                   # an upstream change invalidated the approval: re-run, then re-approve
    RECHECK = "recheck"               # pair-level checks re-running; the approval survives if they pass (§2 S21)
    WAITING_MANUAL = "waiting_manual" # Tripo pack or polish pack is out; waiting for an import
    BUILDING = "building"
    BUILT = "built"                   # build outputs exist and passed the file gate (G3)
    FAILED = "failed"                 # stop rule reached with no acceptable version; the user must act

class DepEffect(StrEnum):
    REGENERATE = "regenerate"         # the AI asset must be generated again (costs money)
    RECOMPOSE = "recompose"           # code-only rebuild: recolour, re-place, re-composite, re-render ($0)
    RECHECK = "recheck"               # re-run checks only; the approval survives if they pass

class DepRule(Strict):
    pattern: str                      # JSON Pointer glob over the spec; "{c}" = this part's character, "*" = one segment
    effect: DepEffect

class LadderState(Strict):
    fixes_used: int = 0               # stop rule: <= 3 per part (ladder.max_fixes_per_part)
    rung: int = 0                     # 0 = not in the ladder; 1..6 (§8.9)
    technique_index: int = 0          # position in the asset's technique ladder (bible §19)
    failed_rungs: dict[str, int] = {} # rung/technique -> failures (a rung that failed twice is skipped)
    best_asset_sha: Sha256 | None = None
    history: list[str] = []           # "rung2:masked_edit:fail", …

class ApprovalRecord(Strict):
    part_id: PartId
    approval_hash: Sha256             # §9.7
    spec_id: str; spec_version: int; spec_slice_sha: Sha256
    input_shas: list[Sha256]; output_shas: list[Sha256]
    pins_sha: Sha256
    approved_at: datetime; decision_id: str

class Part(Strict):
    id: PartId; project_id: str
    character: Literal["a", "b", "duo"]
    kind: PartKind
    label: str                        # "A · plush koi (collar)"
    deps: list[DepRule]               # from the §9.8 table
    part_deps: list[PartId] = []      # other parts whose outputs feed this one (a.shirt <- a.print.top.0)
    route: str                        # "R1", "I3", "I2", "I4+T1", "I5+T1", "I6+slab", "primitive", "compositor",
                                      # "kit", "tripo_api", "tripo_manual", "manual_model", "face_layers", "head_base"
    state: PartState = PartState.PLANNED
    board_assets: dict[str, Sha256] = {}      # role -> asset shown at Gate 2 ("view_front", "tone_sheet", …)
    alternatives: list[dict[str, Sha256]] = []  # other passing finals the user can pick (face top 2-3, print variants)
    build_assets: dict[str, Sha256] = {}      # role -> final files ("mesh_gltf", "mesh_bin", "mesh_fbx", "texture", …)
    approval: ApprovalRecord | None = None
    ladder: LadderState = LadderState()
    open_warnings: list[str] = []     # SOFT CheckResult ids (<= 2 shown, after the first choice)
    license: Literal["n/a", "tripo_api_private_commercial", "tripo_paid_private_commercial",
                     "tripo_free_public_ccby_noncommercial", "unknown"] = "n/a"
    flags: list[str] = []             # "views_from_gpt", "procedural_folds", "no_head_base", "degraded_clone_check", …
    version: int = 0                  # optimistic lock for gate decisions
```

### 6.7 `Asset`, `AssetLink`, `Provenance`

An **asset** is a file, addressed by its content. An **asset link** says what the file is for a given project, part and step. The same bytes can be linked several times (for example a cached result reused by a later spec version).

```python
# duoskin/models/asset.py
AssetKind = Literal["png", "jpeg", "webp", "svg", "glb", "gltf", "bin", "fbx", "obj_zip", "blend", "json", "txt",
                    "luau", "zip", "npz", "html"]

class Provenance(Strict):
    source: Literal["openai", "recraft", "tripo_api", "tripo_manual", "gemini", "anthropic", "code", "kit",
                    "user", "human_polish", "mock"]
    stream: Literal["pipeline", "drill", "regression", "showcase"] = "pipeline"   # data-stream separation
    step_id: str | None = None; step_kind: str | None = None; handler_version: int | None = None
    provider: str | None = None
    model: str | None = None                       # requested snapshot, e.g. "gpt-image-2.5-sunburst-2026-09-08"
    served_model: str | None = None                # the model that answered (Claude fallback) or the API echo
    prompt_id: str | None = None; prompt_version: int | None = None; prompt_sha256: Sha256 | None = None
    params: dict[str, Any] = {}                    # size, quality, background, n, seeds, Recraft controls, Tripo body
    input_shas: list[Sha256] = []                  # ordered; images hashed on decoded RGBA pixels
    mask_sha: Sha256 | None = None
    nonce: str = ""
    request_id: str | None = None                  # OpenAI r._request_id, Anthropic request id, Recraft image_id
    remote_task_id: str | None = None              # Tripo task_id (the envelope request_id goes in params)
    usage: dict[str, Any] | None = None
    cost_usd: float | None = None; cost_basis: Literal["estimate", "usage", "credits"] | None = None
    raw_sha256: Sha256 | None = None               # untouched provider bytes (C2PA / SynthID kept)
    check_ids: list[str] = []                      # CheckResult row ids
    license: str = "n/a"
    notes: list[str] = []                          # "synthid", "views_from_gpt", "degraded_clone_check", …
    created_at: datetime

class Asset(Strict):
    sha256: Sha256                                 # of the file bytes
    pixel_sha: Sha256 | None = None                # images: sha of size + decoded RGBA pixels (cache identity)
    kind: AssetKind; mime: str; bytes: int
    width: int | None = None; height: int | None = None
    tris: int | None = None                        # meshes, after triangulation
    first_provenance: Provenance

class AssetLink(Strict):
    id: str; asset_sha: Sha256
    project_id: str | None; part_id: PartId | None; step_id: str | None
    role: str      # "draft", "final", "concept_of_record", "view_left", "mesh_raw", "mesh_repaired", "render_front", …
    status: Literal["candidate", "rejected", "chosen", "final", "approved", "superseded", "export"]
    rank: int | None = None
    provenance: Provenance                          # provenance in this context (may differ from first_provenance)
```

Rules:
- The CAS writes each file once (`cas\<sha[:2]>\<sha>.<ext>`, temp file + `os.replace`).
- Assets are served at `/cas/<sha>.<ext>` with `Cache-Control: public, max-age=31536000, immutable`, `X-Content-Type-Options: nosniff` and `Content-Security-Policy: sandbox`, after a regex check on the hash.
- Assets with `source="mock"` are refused by the export (CHK-E01).
- Assets with `stream ∈ {drill, regression, showcase}` are never read by the registries, the taste profile or the critic inputs (§3.8).

### 6.8 `Job` and `Step`

```python
# duoskin/models/job.py
class JobKind(StrEnum):
    SETUP = "setup"                 # house-style bootstrap S0, taste profile L2
    LIBRARY = "library"             # fabric (I7) and shading-panel (I8) library builds
    PLAN = "plan"                   # L1..L6, C1, I1 concept drafts, C2 -> Gate 1 -> C3
    PARTS = "parts"                 # part-board asset loops -> Gate 2
    BUILD = "build"                 # clothing templates, head texture, hair, accessories, body
    MANUAL_MESH = "manual_mesh"     # pack export -> MANUAL_IMPORT gate -> import -> mesh gate (one per part)
    DUO = "duo"                     # C5 renders, duo checks, L12-L14 -> Gate 3
    EXPORT = "export"
    CALIBRATION = "calibration"     # drill renders, judge calibration runs
    REGRESSION = "regression"       # 40-brief regression + variety guard

class JobState(StrEnum):
    RUNNING = "running"; WAITING_USER = "waiting_user"; PAUSED = "paused"
    SUCCEEDED = "succeeded"; FAILED = "failed"; CANCELLED = "cancelled"

class Job(Strict):
    id: str; project_id: str | None; kind: JobKind; state: JobState
    spec_id: str | None; params: dict[str, Any] = {}
    created_at: datetime; finished_at: datetime | None = None
    parent_job_id: str | None = None

class StepState(StrEnum):
    PENDING = "pending"                 # waiting for dependencies
    READY = "ready"                     # claimable
    RUNNING = "running"                 # leased by this process
    WAITING_REMOTE = "waiting_remote"   # remote task submitted (remote_ref stored); polled with backoff
    WAITING_USER = "waiting_user"       # a gate or tile is open
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    SUPERSEDED = "superseded"           # its result is no longer used (an upstream change replaced it); kept for history

class StepError(Strict):
    kind: str                           # providers.base.ErrorKind (§7.1)
    code: str | None; message: str
    retryable: bool; billed: Literal["no", "yes", "unknown"]
    provider_request_id: str | None; user_hint: str

class Step(Strict):
    id: str; job_id: str; project_id: str | None
    part_id: PartId | None
    kind: str                           # registry key, e.g. "img.draft", "tripo.multiview" (§8.2)
    handler_version: int
    pool: Literal["api", "cpu", "proc", "none"]
    state: StepState
    deps: list[str] = []                # step ids
    params: dict[str, Any] = {}         # validated by the handler's Params model
    inputs: list[Sha256] = []           # ordered asset shas
    outputs: list[Sha256] = []
    result: dict[str, Any] = {}         # small JSON result (ranking, check ids, chosen draft, validation errors)
    cache_key: Sha256 | None = None
    cached: bool = False
    nonce: str = ""
    paid: bool = False                  # true for any call that can cost money
    attempt: int = 0; max_attempts: int = 3
    not_before: datetime | None = None
    lease_until: datetime | None = None
    lease_owner: str | None = None      # process instance id
    remote_ref: str | None = None       # Tripo task id, batch id, …
    remote_state: Literal["none", "submitted", "submission_uncertain", "polling", "slow", "done"] = "none"
    child_pid: int | None = None; child_create_time: float | None = None
    progress: float = 0.0; message: str = ""
    error: StepError | None = None
    cost_estimate_usd: float = 0.0
    priority: int = 100                 # lower runs first; the tile the user is looking at gets 10
    created_at: datetime; started_at: datetime | None = None; finished_at: datetime | None = None
```

### 6.9 Gates, tiles, decisions and change requests

```python
# duoskin/models/gate.py
class GateKind(StrEnum):
    CONCEPT = "concept"                 # Gate 1
    PART_BOARD = "part_board"           # Gate 2
    FINAL_PICK = "final_pick"           # Gate 3
    BUDGET = "budget"                   # an estimate would pass the cap or the ask threshold
    MANUAL_IMPORT = "manual_import"     # waiting for a user-made mesh (Tripo website, polish)
    CHANGE_CONFIRM = "change_confirm"   # the L7 diff + redo list + estimate; the user confirms or cancels
    CLARIFY = "clarify"                 # L7 needs_clarification
    HUMAN_REVIEW = "human_review"       # ladder rung 6, or a rung-5 notice (plan revision needed)
    SETUP_APPROVAL = "setup_approval"   # house-style exemplars, library admission

class GateAction(StrEnum):
    APPROVE = "approve"; APPROVE_ALL = "approve_all"
    REIMAGINE = "reimagine"             # same plan/spec, new nonce
    CHANGE = "change"                   # typed change -> L7 -> CHANGE_CONFIRM
    NEW_PLAN = "new_plan"               # Gate 1 only
    BACK_TO_CONCEPT = "back_to_concept" # Gate 2 only
    SELECT_ALTERNATIVE = "select_alternative"   # another draft, final, face, plan or duo candidate shown on the tile
    MAKE_MANUAL = "make_manual"         # hair/accessory: "Make it myself on Tripo" (exports the pack)
    PICK = "pick"                       # Gate 3 winner
    EXPORT = "export"                   # Gate 3
    OVERRIDE_WARNING = "override_warning"
    CONFIRM = "confirm"; CANCEL = "cancel"      # CHANGE_CONFIRM, CLARIFY, SETUP_APPROVAL
    CONTINUE = "continue"; RAISE_CAP = "raise_cap"; STOP = "stop"   # BUDGET

class TileState(StrEnum):
    GENERATING = "generating"; READY = "ready"; APPROVED = "approved"; STALE = "stale"; RECHECK = "recheck"
    NEEDS_HUMAN = "needs_human"; WAITING_MANUAL = "waiting_manual"; FAILED = "failed"

class GateTile(Strict):
    tile_id: str                        # Gate 1: "plan0".."plan2"; Gate 2: a part id; Gate 3: a candidate id
    part_id: PartId | None
    label: str
    state: TileState
    assets: dict[str, Sha256]           # role -> asset shown
    alternatives: list[dict[str, Sha256]] = []
    facts: dict[str, Any] = {}          # cost so far, match score, tris, check summary (warnings withheld until the first choice)
    badges: list[str] = []              # "Wildcard", "Views from GPT (lower reliability)", "2D preview — no head base", …
    allowed_actions: list[GateAction]
    version: int = 0

class Gate(Strict):
    id: str; project_id: str; job_id: str; kind: GateKind
    state: Literal["open", "decided", "superseded"]
    tiles: list[GateTile]
    spent_usd_at_open: float
    first_choice_at: datetime | None = None     # warnings are released after this (§9.9)
    opened_at: datetime; decided_at: datetime | None = None

class GateDecisionIn(Strict):           # API request body
    tile_id: str
    action: GateAction
    text: str = Field(default="", max_length=1000)     # "Change…" text or New-plan reasons (data, never instructions)
    mask_sha: Sha256 | None = None      # brush mask for a local edit, uploaded first via /api/uploads
    choice: str | None = None           # alternative index, plan id, candidate id, warning id or new cap
    expected_version: int               # optimistic lock: 409 if the tile changed (ENG-11)
    client_decision_id: str             # idempotency key (a double-click never spawns twice)

class GateDecision(Strict):             # stored
    id: str; gate_id: str; tile_id: str; action: GateAction
    text: str; mask_sha: Sha256 | None; choice: str | None
    decided_at: datetime
    warnings_shown: list[str] = []      # SOFT warning ids shown AFTER the first choice
    warnings_overridden: list[str] = [] # logged as calibration labels
    change_request_id: str | None = None
    resulting_spec_id: str | None = None
    spawned_step_ids: list[str] = []
    approval: ApprovalRecord | None = None

class ChangeRequest(Strict):
    id: str; project_id: str; gate_id: str; tile_id: str | None
    text: str                           # raw user text
    mask_sha: Sha256 | None
    plan: dict[str, Any] | None         # the L7 ChangePlan JSON
    status: Literal["interpreting", "needs_clarification", "awaiting_confirm", "applied", "cancelled", "rejected"]
    invalidation: dict[str, Any] | None # InvalidationReport (§9.8)
    estimate_usd: float | None
```

### 6.10 Costs and budget

```python
# duoskin/models/cost.py
class CostUnit(Strict):
    name: Literal["input_tokens", "output_tokens", "cache_read_tokens", "cache_write_5m_tokens", "cache_write_1h_tokens",
                  "image_input_tokens", "image_output_tokens", "images", "credits", "api_units", "seconds"]
    qty: float
    unit_price_usd: float

class CostEntry(Strict):
    id: str; ts: datetime
    project_id: str | None; step_id: str | None; part_id: PartId | None
    attempt: int = 0
    provider: Literal["anthropic", "openai", "recraft", "tripo", "gemini", "fal", "mock"]
    model: str
    operation: str                      # "messages.stream:L3", "images.edit:I2", "multiview_to_model", …
    units: list[CostUnit] = []
    usd: float
    credits: float | None = None        # Tripo credits (1 credit ≈ $0.01 [third-party])
    basis: Literal["estimate", "usage", "credits", "orphan"]
    state: Literal["reserved", "committed", "released", "orphan"]
    batch: bool = False                 # Anthropic batch discount applied
    price_table: str                    # "prices.json@2026-09-29"
    request_id: str | None = None; remote_task_id: str | None = None
    balance_before: float | None = None; balance_after: float | None = None   # Tripo

class Budget(Strict):
    project_id: str
    cap_usd: float; spent_usd: float; reserved_usd: float
    ask_above_usd: float
    tripo_available_credits: float | None     # balance − frozen (conservative until FM-T4)
    @property
    def remaining(self) -> float: return self.cap_usd - self.spent_usd - self.reserved_usd
```

`duoskin/data/prices.json` (dated) holds the per-unit prices from bible §20.1:
- Claude: Opus 5 $5/$25 per M tokens, Sonnet 5 $2/$10; cache read 0.1×; cache write 1.25× (5 min) or 2× (1 h); batch −50%.
- GPT Image 2.5: text in $5/M, image in $8/M, image out $30/M.
- Recraft: V4.1 vector $0.08, V4 styles vector $0.05, style creation $0.005, vectorize and removeBackground $0.01.
- Tripo: 1 credit ≈ $0.01 [third-party].
- Gemini 3.8 Flash: $0.75/$3.75 per M tokens until 2026-12-31, then $1.50/$7.50.

Every estimate names the price table it used.

### 6.11 `CheckResult`

Exactly FAILURE_MODES §5.1 (`duoskin/checks/model.py`):
- `check_id`, `fm_ids`, `subject_sha`;
- `kind` (`hard | soft | assert`);
- `passed`, `metric`, `value`;
- `threshold` (with the threshold name and status);
- `evidence`;
- `ran` (False ⇒ `passed` must be False: fail closed);
- `fix_hint`;
- `thresholds_version`.

`gate_verdict(results)` fails on any hard or assert failure and never averages. The `kind` comes from `checks/policy.py` (§3.1) plus `settings.check_overrides` (demotions). Results are stored in the `checks` table with their owning step and asset.

### 6.12 Events

```python
class Event(Strict):
    id: int                      # SQLite rowid = SSE id
    ts: datetime
    project_id: str | None
    type: Literal["step.state", "step.progress", "job.state", "gate.opened", "gate.updated", "tile.updated",
                  "part.state", "spec.updated", "cost.added", "budget.low", "project.stage", "llm.thinking",
                  "inbox.file", "doctor.result", "toast", "warning.released", "server.restarting"]
    payload: dict[str, Any]      # small; assets referenced by sha only
```

- Progress events are throttled to ≤2 per second per step.
- `llm.thinking` carries at most 400 characters of summarized thinking (L3 only) and is kept for 24 h.
- Events older than 30 days are pruned.

### 6.13 Registries, memory, labels

| Table | Row | Rule |
|---|---|---|
| `registry_face` | project_id, character, part_role, asset_sha, pixel_sha, phash, embedding (blob or null), grammar_json, duo_seq, listed, registered_at | Exact pixel reuse is blocked forever. Near-duplicates (pHash ≤8, DreamSim <0.15 [CALIBRATE]) are blocked within the last 30 duos, or against rows with `listed=1` (§3.7) |
| `registry_print` | same shape, for prints and badge art | same rule |
| `dna_cards` | spec_id, project_id, version, card_json, created_at | The last 5 go to the planner as a hint (§3.2) |
| `duo_memory` | project_id, dna_card_json, kit_ids_json, render_embedding (4-side), approved_at | Written after Gate 3. The nearest-duo distance is a SOFT warning and a tie-breaker only |
| `recently_used` | a derived view over the last 10 approved duos: hair kit ids, eye shapes, mouth styles, palette families, fabric ids, pair structures, anchor kinds | A planner hint only (never a lint) |
| `labels` | id, kind (`clone_real_stranger`, `like_dislike`, `rule_verdict`, `warning_override`), subject_ids, value, source (`gate`, `drill`, `calibration`), ts | Drill labels ≤25% of the calibration set; never feed the taste profile |
| `check_stats` | check_id, window, flagged_approved, flagged_rejected, shown, overridden | Feeds warning visibility and the weekly report (§3.1) |

### 6.14 SQLite schema (DDL, `db/schema/001_init.sql`)

```sql
PRAGMA foreign_keys = ON;
CREATE TABLE projects    (id TEXT PRIMARY KEY, json TEXT NOT NULL, stage TEXT NOT NULL, updated_at TEXT NOT NULL,
                          version INTEGER NOT NULL DEFAULT 0) STRICT;
CREATE TABLE specs       (id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id), plan_set_id TEXT NOT NULL,
                          version INTEGER NOT NULL, status TEXT NOT NULL, sha256 TEXT NOT NULL, json TEXT NOT NULL,
                          created_at TEXT NOT NULL) STRICT;
CREATE TABLE dna_cards   (spec_id TEXT NOT NULL, version INTEGER NOT NULL, project_id TEXT NOT NULL, json TEXT NOT NULL,
                          created_at TEXT NOT NULL, PRIMARY KEY (spec_id, version)) STRICT;
CREATE TABLE parts       (project_id TEXT NOT NULL REFERENCES projects(id), id TEXT NOT NULL, state TEXT NOT NULL,
                          json TEXT NOT NULL, version INTEGER NOT NULL DEFAULT 0, PRIMARY KEY (project_id, id)) STRICT;
CREATE TABLE assets      (sha256 TEXT PRIMARY KEY, pixel_sha TEXT, kind TEXT NOT NULL, mime TEXT NOT NULL,
                          bytes INTEGER NOT NULL, width INTEGER, height INTEGER, tris INTEGER, json TEXT NOT NULL,
                          created_at TEXT NOT NULL) STRICT;
CREATE TABLE asset_links (id TEXT PRIMARY KEY, asset_sha TEXT NOT NULL REFERENCES assets(sha256), project_id TEXT,
                          part_id TEXT, step_id TEXT, role TEXT NOT NULL, status TEXT NOT NULL, stream TEXT NOT NULL,
                          rank INTEGER, json TEXT NOT NULL, created_at TEXT NOT NULL) STRICT;
CREATE INDEX asset_links_part ON asset_links(project_id, part_id, role);
CREATE TABLE jobs        (id TEXT PRIMARY KEY, project_id TEXT, kind TEXT NOT NULL, state TEXT NOT NULL,
                          json TEXT NOT NULL, created_at TEXT NOT NULL) STRICT;
CREATE TABLE steps       (id TEXT PRIMARY KEY, job_id TEXT NOT NULL REFERENCES jobs(id), project_id TEXT, part_id TEXT,
                          kind TEXT NOT NULL, state TEXT NOT NULL, pool TEXT NOT NULL, priority INTEGER NOT NULL,
                          not_before TEXT, lease_until TEXT, lease_owner TEXT, remote_ref TEXT, cache_key TEXT,
                          paid INTEGER NOT NULL, json TEXT NOT NULL, created_at TEXT NOT NULL) STRICT;
CREATE INDEX steps_claim ON steps(state, pool, priority, not_before);
CREATE TABLE step_deps   (step_id TEXT NOT NULL, dep_id TEXT NOT NULL, PRIMARY KEY (step_id, dep_id)) STRICT;
CREATE TABLE cache       (cache_key TEXT PRIMARY KEY, step_kind TEXT NOT NULL, outputs TEXT NOT NULL, result TEXT NOT NULL,
                          created_at TEXT NOT NULL, last_hit_at TEXT) STRICT;
CREATE TABLE checks      (id TEXT PRIMARY KEY, project_id TEXT, step_id TEXT, subject_sha TEXT NOT NULL,
                          check_id TEXT NOT NULL, kind TEXT NOT NULL, passed INTEGER NOT NULL, json TEXT NOT NULL,
                          created_at TEXT NOT NULL) STRICT;
CREATE TABLE gates       (id TEXT PRIMARY KEY, project_id TEXT NOT NULL, job_id TEXT NOT NULL, kind TEXT NOT NULL,
                          state TEXT NOT NULL, json TEXT NOT NULL, opened_at TEXT NOT NULL, decided_at TEXT) STRICT;
CREATE TABLE decisions   (id TEXT PRIMARY KEY, gate_id TEXT NOT NULL REFERENCES gates(id), tile_id TEXT NOT NULL,
                          action TEXT NOT NULL, client_decision_id TEXT NOT NULL UNIQUE, json TEXT NOT NULL,
                          decided_at TEXT NOT NULL) STRICT;
CREATE TABLE approvals   (project_id TEXT NOT NULL, part_id TEXT NOT NULL, approval_hash TEXT NOT NULL,
                          decision_id TEXT NOT NULL, valid INTEGER NOT NULL, json TEXT NOT NULL, created_at TEXT NOT NULL,
                          PRIMARY KEY (project_id, part_id, approval_hash)) STRICT;
CREATE TABLE changes     (id TEXT PRIMARY KEY, project_id TEXT NOT NULL, status TEXT NOT NULL, json TEXT NOT NULL,
                          created_at TEXT NOT NULL) STRICT;
CREATE TABLE cost_ledger (id TEXT PRIMARY KEY, project_id TEXT, step_id TEXT, attempt INTEGER NOT NULL DEFAULT 0,
                          operation TEXT NOT NULL, provider TEXT NOT NULL, state TEXT NOT NULL, usd REAL NOT NULL,
                          credits REAL, request_id TEXT, json TEXT NOT NULL, ts TEXT NOT NULL) STRICT;
CREATE UNIQUE INDEX cost_once ON cost_ledger(step_id, attempt, operation) WHERE step_id IS NOT NULL;   -- ENG-03
CREATE TABLE events      (id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, project_id TEXT, type TEXT NOT NULL,
                          payload TEXT NOT NULL) STRICT;
CREATE TABLE registry_face  (id TEXT PRIMARY KEY, project_id TEXT NOT NULL, asset_sha TEXT NOT NULL, pixel_sha TEXT NOT NULL,
                             phash TEXT NOT NULL, embedding BLOB, json TEXT NOT NULL, duo_seq INTEGER NOT NULL,
                             listed INTEGER NOT NULL DEFAULT 0, registered_at TEXT NOT NULL) STRICT;
CREATE TABLE registry_print (id TEXT PRIMARY KEY, project_id TEXT NOT NULL, asset_sha TEXT NOT NULL, pixel_sha TEXT NOT NULL,
                             phash TEXT NOT NULL, embedding BLOB, json TEXT NOT NULL, duo_seq INTEGER NOT NULL,
                             listed INTEGER NOT NULL DEFAULT 0, registered_at TEXT NOT NULL) STRICT;
CREATE TABLE duo_memory  (project_id TEXT PRIMARY KEY, embedding BLOB, json TEXT NOT NULL, approved_at TEXT NOT NULL) STRICT;
CREATE TABLE labels      (id TEXT PRIMARY KEY, kind TEXT NOT NULL, source TEXT NOT NULL, json TEXT NOT NULL, ts TEXT NOT NULL) STRICT;
CREATE TABLE check_stats (check_id TEXT NOT NULL, window_start TEXT NOT NULL, json TEXT NOT NULL,
                          PRIMARY KEY (check_id, window_start)) STRICT;
CREATE TABLE inbox       (path TEXT PRIMARY KEY, sha256 TEXT, size INTEGER, state TEXT NOT NULL, assigned_part TEXT,
                          json TEXT NOT NULL, seen_at TEXT NOT NULL) STRICT;
CREATE TABLE child_procs (pid INTEGER NOT NULL, create_time REAL NOT NULL, step_id TEXT, started_at TEXT NOT NULL,
                          PRIMARY KEY (pid, create_time)) STRICT;
```

**Connection rules** (ENG-13):
- one connection per thread (`timeout=10`, `isolation_level=None`);
- PRAGMAs: `journal_mode=WAL`, `synchronous=NORMAL`, `busy_timeout=5000`, `foreign_keys=ON`;
- every write transaction starts with `BEGIN IMMEDIATE`;
- migrations are tracked with `PRAGMA user_version`;
- `PRAGMA integrity_check` and `Connection.backup()` at startup (5 rotating copies in `DATA\backups\`);
- `wal_checkpoint(TRUNCATE)` on a clean shutdown.

---

## 7. Provider layer

Every external API sits behind a small interface. Each interface has a **real** implementation and a **mock** with the same signature. `providers/registry.py` picks one per provider from Settings (`real | mock | disabled`), so the user can, for example, run real Claude with a mock Tripo. Pipeline code never imports an SDK directly.

**Routing when a key is missing** (bible D22, §2 S17):

| Missing key | Effect |
|---|---|
| Anthropic, OpenAI | The pipeline cannot run real jobs. Only demo (mock) mode is available; the Settings page says so. |
| Tripo | T1 multiview is replaced by I10 GPT views (flagged "Views from GPT, lower reliability"); T3 is replaced by manual mode only. |
| Recraft | Face parts default to I3, prints to I2, badges to I6; the Recraft rungs (R1, R2, vectorize, removeBackground) are skipped on the ladders; background removal uses local matting. |
| Gemini | G1 second opinions use a second Sonnet 5 juror (L11 route); G2 is removed from the ladders. |
| fal | Nothing changes (no step uses fal). |

### 7.1 Common base (`providers/base.py`)

```python
ErrorKind = Literal[
    "auth",                 # 401: show the key dialog
    "billing",              # 402 / insufficient_quota: pause the whole queue
    "permission",           # 403 (e.g. OpenAI "organization must be verified")
    "not_found",            # 404 / retired model id (Tripo 2015 [UNVERIFIED]): "update the model ID"
    "bad_request",          # 400 code bug: never retry
    "capability",           # 400 unknown parameter / unsupported combination: drop the param, store the flag
    "moderation",           # OpenAI moderation_blocked, Tripo 2008, Gemini IMAGE_SAFETY: rewrite once, then the user
    "recitation",           # Gemini IMAGE_RECITATION: originality failure -> back to change or plan
    "refusal",              # Claude stop_reason == "refusal"
    "truncated",            # Claude max_tokens / model_context_window_exceeded
    "validation",           # structured output failed Pydantic: goes to the reviser/repair path (counts as a round)
    "schema_too_complex",   # Claude 400 "Schema is too complex for compilation": kit enums -> str (bible §3.1)
    "rate_limit",           # 429 (Tripo 1007): back off; honour retry-after / X-RateLimit-Reset
    "concurrency",          # Tripo 429 + 2000: wait for our own tasks, lower the slots
    "overloaded", "server", # 529 / 5xx: backoff
    "timeout", "network",   # client side
    "submission_uncertain", # paid POST whose body was sent but whose answer was lost: reconcile, never resend
    "remote_failed",        # remote task failed (Tripo failed/cancelled/banned/expired/unknown)
    "other"]

class ProviderError(Exception):
    def __init__(self, provider: str, kind: ErrorKind, message: str, *, http: int | None = None,
                 code: str | None = None, retryable: bool = False, billed: Literal["no", "yes", "unknown"] = "unknown",
                 request_id: str | None = None, user_hint: str = "", retry_after_s: float | None = None): ...

@dataclass(frozen=True)
class CallCtx:                  # passed in by the step context; lets providers heartbeat and honour cancel
    step_id: str | None
    heartbeat: Callable[[], None]
    check_cancel: Callable[[], None]          # raises Cancelled
    progress: Callable[[float, str], None]

class RateLimiter:              # token bucket + semaphore per provider, shared by all threads
    def __init__(self, *, rpm: float | None, rps: float | None, ipm: float | None, concurrent: int): ...
    def acquire(self, *, images: int = 0, ctx: CallCtx) -> ContextManager[None]: ...
    def penalize(self, seconds: float) -> None: ...     # after a 429 everyone waits
```

**Clients, timeouts and retries** (FAILURE_MODES X8):

| Provider | Client | SDK retries | App retries |
|---|---|---|---|
| Anthropic | `Anthropic(api_key=…, max_retries=2)`; every call streamed | 2 (connection, 408, 409, 429, 5xx incl. 529) | Queue backoff with jitter, ≤3 attempts per step; `truncated` once with 2× `max_tokens` |
| OpenAI Images | `OpenAI(api_key=…, timeout=900, max_retries=0)` | **0** (a timeout followed by an SDK retry may bill twice [UNVERIFIED]) | 5xx ≤2 with backoff; timeout ≤1, logged |
| Recraft | httpx2 client, timeout 120 s | — | 429 backoff with jitter; 5xx ≤2 |
| Tripo | httpx2 API client (timeout 60 s, Bearer) plus a separate download client (timeout 120 s, **no** auth header, no redirects, host allowlist, 150 MB cap) | — | §7.5 retry table; paid POSTs are never resent |
| Gemini | google-genai 2.25.x behind the adapter (`generateContent`), or httpx2 REST | SDK default | 1 retry on an empty `resp.text` |

**Capability flags** are stored in `settings.capabilities`. They are set by the probes (§15.4, bible §8.1.6, FM-T6) or by the first error that proves them:
- `openai.mask_multi_ok`, `openai.rgba_image1_ok`, `openai.usage_present`;
- `recraft.background_color_honoured`, `recraft.file_field_name` (`file` or `image`);
- `tripo.orthographic_projection`, `tripo.texture_version_delight`, `tripo.balance_excludes_frozen`, `tripo.convert_on_file_token`;
- `anthropic.sonnet_fallbacks`, `anthropic.schema_ok.<route>`;
- `studio.gltf_embedded_ok`, `studio.glb_ok`, `studio.forward_axis` (from the user's calibration import).

An unknown flag always selects the conservative path.

**Secrets in transit.** Keys are read with `keystore.get_key()` when the client is built. They are never logged, never put in provenance and never sent to the browser; the settings API returns only `sk-…abcd`. Tripo signed URLs and Recraft image URLs are not logged either.

### 7.2 Claude (`providers/anthropic_llm.py`)

```python
Route = Literal["L1_reference", "L2_taste", "L3_planner", "L4_critic", "L5_pairwise", "L6_reviser", "L7_change",
                "L9_hair_match", "L10_repair", "L11_checker", "L12_duo_judge", "L12_duo_review", "L13_ip",
                "L14_similarity", "smoke"]

@dataclass(frozen=True)
class RouteCfg: model: str; effort: str; max_tokens: int; fallbacks: bool

ROUTES: dict[Route, RouteCfg]   # bible §9.0; model ids come from the project's VersionPins

@dataclass
class LLMResult(Generic[T]):
    parsed: T                                # validated model (never None on return; errors raise ProviderError)
    stop_reason: str                         # always "end_turn" on success
    raw_text: str
    usage: dict[str, int]                    # input, output, cache_read_input_tokens, cache_creation_input_tokens
    request_id: str | None
    requested_model: str; served_model: str  # different when a fallback answered
    thinking_summary: str                    # display="summarized"; UI only; kept 24 h
    schema_hash: str; prompt_version: int

class LLMProvider(Protocol):
    def call(self, route: Route, *, system: list[dict], content: list[dict], out: type[T], ctx: CallCtx,
             prompt_version: int, max_tokens_override: int | None = None) -> LLMResult[T]: ...
    def upload_file(self, data: bytes, *, name: str, mime: str) -> str: ...          # Files API -> file_id
    def delete_file(self, file_id: str) -> None: ...
    def batch_submit(self, items: list[BatchItem]) -> str: ...                       # no fallbacks on batches
    def batch_status(self, batch_id: str) -> Literal["in_progress", "ended", "canceled", "expired"]: ...
    def batch_results(self, batch_id: str) -> Iterator[BatchItemResult]: ...
    def capabilities(self) -> dict[str, Any]: ...          # models.retrieve(id).capabilities for both models
    def allowed_fallbacks(self, model: str) -> list[str]: ...  # beta models.retrieve with server-side-fallback-2026-06-01
    def smoke_test(self, route: Route, out: type[BaseModel]) -> CheckResult: ...     # CHK-S10
```

**Implementation contract** (bible §2.8 and §9.0; §2 S15):

```python
def call(self, route, *, system, content, out, ctx, prompt_version, max_tokens_override=None):
    cfg = ROUTES[route]
    kw = dict(model=cfg.model, max_tokens=max_tokens_override or cfg.max_tokens,
              thinking={"type": "adaptive", "display": "summarized"},
              output_config={"effort": cfg.effort,
                             "format": {"type": "json_schema", "schema": SCHEMA_CACHE[out]}},
              system=system, messages=[{"role": "user", "content": content}])
    if cfg.fallbacks:                                   # Opus routes; Sonnet only if allowed_fallbacks() is non-empty
        stream = self.client.beta.messages.stream(betas=["server-side-fallback-2026-07-01"], fallbacks="default", **kw)
    else:
        stream = self.client.messages.stream(**kw)
    with self.limiter.acquire(ctx=ctx), stream as s:
        thinking = []
        for ev in s:
            ctx.heartbeat(); ctx.check_cancel()
            if ev.type == "content_block_delta" and ev.delta.type == "thinking_delta":
                thinking.append(ev.delta.thinking)
        r = s.get_final_message(); rid = s.request_id
    self.ledger.record_usage(route, cfg.model, r.model, r.usage, rid)     # CostEntry basis="usage"
    if r.stop_reason == "refusal":                                        # branch on stop_reason, never stop_details
        raise ProviderError("anthropic", "refusal", str(r.stop_details), request_id=rid, billed="yes")
    if r.stop_reason in ("max_tokens", "model_context_window_exceeded"):
        raise ProviderError("anthropic", "truncated", r.stop_reason, retryable=True, request_id=rid, billed="yes")
    text = next((b.text for b in r.content if b.type == "text"), None)    # thinking blocks come first
    if text is None:
        raise ProviderError("anthropic", "truncated", "no text block", retryable=True, request_id=rid)
    try:
        parsed = out.model_validate_json(text)          # enums lowercased by the BeforeValidator
    except ValidationError as e:
        raise ProviderError("anthropic", "validation", e.json(), request_id=rid, billed="yes") from e
    return LLMResult(parsed, r.stop_reason, text, …, requested_model=cfg.model, served_model=r.model, …)
```

- `SCHEMA_CACHE[M] = make_all_required(anthropic.transform_schema(M))`, built once per class. Schemas that contain kit enums are rebuilt when `kit_manifest_sha` changes.
- Never send `temperature`, `top_p`, `top_k`, `budget_tokens` or an assistant prefill (400 on these models). Never use `messages.parse()` in a pipeline step (it raises before `stop_reason` can be read; FAILURE_MODES X18).
- **System blocks** come from `prompts/system_blocks.py` in a fixed order: `SHARED_CONTEXT`, `ROBLOX_RULES`, `STYLE_GUIDE`, `KIT_INVENTORY` (sorted canonical JSON), then the role text. `cache_control: {"type": "ephemeral"}` goes on the role block, with `"ttl": "1h"` while a gate is open. User content: the cached style references by `file_id` (a second breakpoint on the last shared image), then the variable data in tags. No timestamps or IDs appear in the cached prefix.
- **Images to Claude:** PNG only; composited on `#808080` **and** on an 8 px checkerboard (the prompt says which is which); nearest-neighbour upscale so each side is ≥256 px; long edge ≤2576 px (≤2000 px when the request carries more than 20 images).
- **Fan-out:** when N calls share a prefix, send one, wait for its first streamed token, then send the other N−1 (a cache entry is readable only after the first response starts).
- **Batches** (overnight re-checks, taste-profile rebuilds): the same params **without** `fallbacks`; refused, errored or expired items are re-run synchronously through `call()`.
- **Cache monitor:** a SOFT alert when a route's second call inside the TTL has `cache_read_input_tokens == 0` (CHK-X04).

### 7.3 OpenAI GPT Image 2.5 (`providers/openai_images.py`)

```python
Quality = Literal["low", "medium", "high", "xhigh", "max"]       # never "auto"

@dataclass(frozen=True)
class NamedPng: name: str; data: bytes                             # sent as (name, bytes, "image/png")

@dataclass(frozen=True)
class ImageRequest:
    model: str                        # pinned snapshot, e.g. "gpt-image-2.5-flare-2026-09-08"
    prompt: str                       # compiled + linted text (CompiledPrompt.text)
    size: str                         # "WxH"; valid_size() asserted before sending
    quality: Quality
    background: Literal["opaque", "transparent"]
    n: int                            # 1..min(settings.openai_ipm, 4) for drafts; 1 (or 2) for finals
    images: tuple[NamedPng, ...] = () # edit only; images[0] is the image being edited (the mask applies to it); <=16
    mask: NamedPng | None = None      # RGBA PNG, same size as images[0], alpha 0 = editable, <4 MB
    output_format: Literal["png"] = "png"
    user: str = "duoskin-local"       # a fixed hashed identifier; never the user's email

@dataclass
class ImageResult:
    images: list[bytes]               # decoded PNG bytes, archived untouched (raw_sha256)
    size: str                         # r.size (asserted == requested)
    usage: dict | None                # r.usage may be None
    request_id: str | None            # r._request_id
    model: str

class ImageGenProvider(Protocol):
    def generate(self, req: ImageRequest, ctx: CallCtx) -> ImageResult: ...   # req.images must be empty
    def edit(self, req: ImageRequest, ctx: CallCtx) -> ImageResult: ...       # req.images non-empty
    def probe(self) -> dict[str, bool]: ...                                   # FM-T6 capability probes
```

**Adapter rules** (CHK-P02, CHK-P08, bible §2.1 and §2.6):
- Always pass `model`, `quality`, `background`, `output_format`, `size`, `n` and `user`. **Omit** `moderation` (§2 S2), `input_fidelity`, `output_compression` and `stream` (streaming only for gate previews, bible U25).
- `images.generate` takes no input images. Anything with a reference, guide or mask uses `images.edit`.
- With a mask **and** more than one image while `mask_multi_ok` is false: drop the mask for opaque Image 1 templates (paste-back and the ring check still run), or drop the extra images for transparent Image 1 templates (bible D17).
- **Image 1 alpha rule (bible U26):** an RGBA `images[0]` with transparent pixels is sent only together with an explicit mask; otherwise it is flattened on `#F2F2F2`, unless `rgba_image1_ok` is set.
- Assert that the returned `r.size` and the decoded W×H equal the request and `images[0]`. A mismatch is a size-drift failure: reject, never resize.
- Decode `b64_json`; normalise to RGBA in code and never convert RGBA to RGB; strip ICC/gAMA chunks from derived assets.
- Error mapping: `moderation_blocked` → `ProviderError(kind="moderation", billed="no")`; `insufficient_quota` → `billing`; an unknown parameter → `capability` (drop the parameter, store the flag); 403 "organization must be verified" → `permission` with the Settings hint.
- A limiter sized to the images-per-minute setting (default 5, reportedly Tier 1 [UNVERIFIED]) counts every image of `n`.

### 7.4 Recraft (`providers/recraft.py`)

```python
@dataclass(frozen=True)
class VectorRequest:
    model: Literal["recraftv4_1_vector", "recraftv4_1_utility_vector", "recraftv4_styles_vector",
                   "recraftv4_1_pro_vector", "recraftv4_1_utility_pro_vector", "recraftv4_styles_pro_vector"]
    prompt: str                       # flat form, <=5 numbered constraints, no hex, no style words when style_id is set
    size: str                         # a V4 preset only (1024x1024, 1536x768, 768x1536, …)
    n: int                            # 1..6
    style_id: str | None              # required by *_styles_* models; never together with style or style_reference_urls
    style_match: Literal["precise", "flexible"] = "precise"
    colors: tuple[tuple[int, int, int], ...] = ()    # controls.colors ("preferable", not guaranteed)
    background_rgb: tuple[int, int, int] | None = None   # the sentinel colour (controls.background_color)

@dataclass
class VectorResult:
    svgs: list[bytes]                 # downloaded at once (URLs live ~24 h) or b64_json
    image_ids: list[str]; credits: float | None; style_id: str | None
    request_json: dict                # stored in provenance (V4 has no seed)

class VectorGenProvider(Protocol):
    def generate(self, req: VectorRequest, ctx: CallCtx) -> VectorResult: ...
    def create_style(self, pngs: list[bytes], *, model: Literal["recraftv4_styles_vector", "recraftv4_styles"],
                     style: Literal["vector_illustration", "any"]) -> str: ...     # POST /styles; SVG not accepted
    def vectorize(self, png: bytes, *, max_num_shapes: int | None = None) -> bytes: ...   # $0.01
    def remove_background(self, png: bytes) -> bytes: ...                                   # $0.01, RGBA
```

- Base URL `https://external.api.recraft.ai/v1`, header `Authorization: Bearer …`, `POST /images/generations` (JSON).
- Never send `negative_prompt`, `no_text`, `artistic_level` or `style` preset names (V4 ignores or rejects them; bible D8).
- Token bucket: 100 images/min and 5 requests/s (≤25 calls/min at n=4); on 429 back off with jitter.
- Keep **separate** raster and vector `style_id` registries (a vector style on a raster model returns SVG bytes).
- Multipart field name for vectorize and removeBackground: try `file`, fall back to `image`, store the flag [UNVERIFIED].
- Every SVG goes through `imaging/svg.py` (§10.4.2) before any pixel of it is used.

### 7.5 Tripo v3 (`providers/tripo.py`)

Our own client on the v3 REST API (`https://openapi.tripo3d.ai/v3`). The official `tripo3d` SDK is not used: it still creates tasks through v2 `/task`, and v2 turns off on 2026-11-01.

```python
View = Literal["front", "left", "back", "right"]      # "left" = the SUBJECT's own left (90°)

class P2Params(Strict):
    model: Literal["P2-20260801"] = "P2-20260801"
    face_limit: int                                    # always sent: plush/bag/small_hat/prop 3000, keychain 1500, hair 3500
    quad: Literal[False] = False
    texture: Literal[True] = True
    texture_quality: Literal["standard"] = "standard"  # 2K; never detailed/extreme
    pbr: Literal[False] = False                        # ComfyUI defaults it to true: always send false
    texture_alignment: Literal["original_image"] = "original_image"
    orientation: Literal["default", "align_image"] = "default"   # align_image only as a test-day A/B arm (bible D21)
    auto_size: Literal[False] = False
    model_seed: int; texture_seed: int                 # 11 -> 29 -> 47, one at a time
    orthographic_projection: bool | None = None        # A/B flag only [UNVERIFIED for P2]
    texture_version: str | None = None                 # A/B flag only, together with delight=False
    delight: bool | None = None
    def body(self) -> dict: ...                        # drops None fields; never emits compress/generate_parts/smart_low_poly

class RemoteStatus(Strict):
    task_id: str
    status: Literal["queued", "running", "success", "failed", "cancelled"]   # legacy banned/expired/unknown -> failed
    progress: int | None; error_code: int | None; error_message: str | None
    credits_consumed: float | None
    input: dict                                        # the parameters actually used (e.g. model_seed)
    output_urls: dict[str, str]                        # model_url, rendered_image_url, view urls (never logged)

class TripoProvider(Protocol):
    def balance(self) -> tuple[float, float]: ...                       # (balance, frozen)
    def usage(self, *, limit: int = 50, offset: int = 0) -> list[dict]: ...
    def upload(self, png: bytes, *, name: str) -> str: ...             # POST /files -> file_token (upload just before use)
    def image_to_multiview(self, token: str) -> str: ...               # 10 credits; image-generation pool (1 slot)
    def edit_multiview(self, mv_task_id: str, prompts: dict[View, str]) -> str: ...   # 5 credits per view; once per set
    def multiview_to_model(self, views: dict[View, str] | str, p: P2Params) -> str: ... # 110 credits; str = reuse an mv task
    def image_to_model(self, token: str, p: P2Params) -> str: ...      # enable_image_autofix=False added by the body builder
    def convert(self, source: str, *, fmt: Literal["GLTF", "FBX", "OBJ"], face_limit: int | None,
                texture_size: int = 1024, texture_format: Literal["PNG"] = "PNG") -> str: ...   # 5-10 credits
    def import_model(self, token: str) -> str: ...                     # free
    def task(self, task_id: str) -> RemoteStatus: ...
    def tasks(self, ids: list[str]) -> tuple[dict[str, RemoteStatus], list[str]]: ...   # POST /tasks/list (<=100)
    def download(self, task_id: str, keys: list[str]) -> dict[str, bytes]: ...  # at once; 403/404 -> re-GET the task (<=3)
    def reconcile_uncertain(self, *, endpoint: str, submitted_at: datetime, body: dict) -> str | None: ...
```

- Views are always sent as named objects: `"inputs": [{"front": tok}, {"left": tok}, {"back": tok}, {"right": tok}]`. Front plus at least one other view.
- Downloads: host allowlist `tripo-data.rg1.data.tripo3d.com` and `*.tripo3d.ai`; **no Bearer header** to storage; 150 MB cap; magic bytes (`glTF` = GLB, `Kaydara FBX Binary` = FBX, `PK` = ZIP); SHA-256; stored in the CAS in the same step that saw `success` (signed URLs last about 5 minutes).
- **Retry table:**

| Situation | Action |
|---|---|
| 429 + code 1007 (rate) | Back off 1→32 s with jitter; use `X-RateLimit-Reset` if present (epoch or delta) |
| 429 + code 2000 (concurrency) | Wait for our own tasks to finish; lower the local slot count by 1 |
| 400/401/403, or codes 2010/2015 [UNVERIFIED meanings] | No retry; tell the user (field, key, credits, model ID) |
| GET network error or 5xx | Up to 5 retries with backoff |
| POST `ConnectError`/`ConnectTimeout` (nothing sent) | Retry |
| **POST `ReadTimeout`, a reset, or a 5xx after the body was sent** | **`submission_uncertain`: never resend.** Reconcile through `/account/usage` (same type, ±2 min) plus `GET /tasks/{id}`, comparing `input.model_seed` |
| Task `failed` + 2008 (moderation) | Stop; show the user; never resubmit the same images |
| Task `failed` + 2018 (queue expiry) | Resubmit once |
| Other task failure | Next seed once, then the user |
| Poll soft timeout (20 min) | Mark "slow" and keep polling; never resubmit (ENG-07) |

- Ledger: before a paid submit, `reserve()` requires `est_credits ≤ min(balance − frozen, budget left)` (conservative until FM-T4 settles `balance`). When the task ends, commit `credits_consumed`.

### 7.6 Gemini (optional; `providers/gemini.py`)

```python
class RuleSpec(Strict): rule_id: str; statement: str
class RuleVerdict(Strict): rule_id: str; evidence: str; passed: bool

class JudgeProvider(Protocol):                          # G1 second opinion
    def judge(self, png: bytes, rules: list[RuleSpec], *, thinking_level: Literal["LOW", "MEDIUM"],
              ctx: CallCtx) -> list[RuleVerdict]: ...   # one image per call; evidence before pass in the schema

class BackupImageProvider(Protocol):                    # G2 Nano Banana 2 (the "other model" rung)
    def generate(self, *, prompt: str, images: list[bytes], aspect: str, size: Literal["1K"],
                 ctx: CallCtx) -> tuple[bytes, Literal["png", "jpeg"], dict]: ...   # never alpha; SynthID noted
```

- Model ids are pinned (`gemini-3.8-flash`, `gemini-3.1-flash-image`); no `-latest` aliases. `generateContent` sits behind the adapter so the Interactions API can replace it later; Interactions calls send `store=False`.
- Judge: `response_mime_type="application/json"`, `response_json_schema`, `thinking_level` LOW or MEDIUM (MINIMAL is invalid on 3.8), `media_resolution="MEDIA_RESOLUTION_HIGH"`; no temperature, top_p or top_k. An empty `resp.text` is a FAIL (retry once).
- Image backup: sniff the magic bytes; skip parts with `part.thought`; `IMAGE_SAFETY`/`IMAGE_PROHIBITED_CONTENT` → `moderation`; `IMAGE_RECITATION` → `recitation`. Transparent templates swap their OUTPUT line to a sentinel background and code unmixes (§10.4). A JPEG is never a final texture.
- Privacy: an image marked private goes to Gemini only when its key is marked "billed" in Settings (CHK-P11).

### 7.7 fal (`providers/fal.py`)

A settings slot and an adapter stub only. No step calls it (bible D23). Settings shows the key as "stored, unused".

### 7.8 Mock providers (`providers/mock/*`)

Mocks are **deterministic**: each output is a function of `sha256(canonical request)`, so golden tests are stable. They record every request for assertions, add `CostEntry` rows with `provider="mock"`, and tag assets `source="mock"` (the export refuses them).

| Mock | Behaviour |
|---|---|
| `MockLLM` | Builds a valid object of the requested schema for each route. **L3** uses `providers/fixtures/plansets/*.json` when the brief matches a fixture name. Otherwise a generator draws kit ids from the live manifest and produces 3 specs that pass C1: 3 different structures, one wildcard, 2 anchors, 5 measurable contrasts, faces differing in ≥3 fields, the garment-cut rule, ≥2 differing DNA fields. **L4/L5/L12** return fixed levels with an order-dependent tie-breaker, so the both-orders logic is exercised. **L11** passes every rule unless a fault is injected. **L7** maps "make … <colour>" to a palette patch and "bigger" to a `size_class` patch; anything else returns `needs_clarification`. A few fake thinking deltas are streamed. |
| `MockImages` | `edit`/`generate` return `n` PNGs of the requested size. Guide-based calls (I1, I4, I8) paint flat colours into the mask area of Image 1, so paste-back and Gate A run for real. Transparent calls draw one centred shape with real alpha. FINALIZE returns a lightly sharpened copy of Image 1 (passes A_DRIFT). `usage` holds realistic token counts. |
| `MockRecraft` | Returns simple valid SVGs (paths with explicit fills on the sentinel background, no gradients), `credits` and image ids; `create_style` returns a fixed UUID. |
| `MockTripo` | A state machine: `queued` → `running` (2 polls) → `success`. Multiview returns 4 views rendered from a trimesh primitive. P2 returns a textured GLB (icosphere or rounded box, ~2k triangles, a 1024 PNG, one material) with `credits_consumed`. `balance` returns (2000, 0). |
| `MockGemini` | The judge passes every rule; the image model returns a JPEG on white (exercises the unmix path). |

Fault injection and demo mode are in §16.

---

## 8. Job engine

### 8.1 Principles

- **Every unit of work is a step**, with typed params, declared inputs, a cache key, a lease, a cost estimate and a result. Steps are immutable history: a change never edits an old step; it spawns new ones and marks the old results `SUPERSEDED`.
- **Resumable.** A crash, sleep or restart loses at most the call in flight; remote tasks resume polling from `remote_ref`.
- **Never pay twice.** The cache is consulted before any spend; `remote_ref` is committed before polling; paid Tripo POSTs are never resent; gate decisions are idempotent (`client_decision_id`).
- **The engine does not know prompts or Roblox.** Handlers do. The engine knows states, leases, caching, costs, gates and the fix ladder.

### 8.2 Step handlers (registry)

```python
# duoskin/engine/registry.py
class StepHandler(Protocol):
    kind: ClassVar[str]                     # registry key
    version: ClassVar[int]                  # bump on any behaviour change -> new cache keys
    pool: ClassVar[Literal["api", "cpu", "proc", "none"]]
    paid: ClassVar[bool]
    provider: ClassVar[str | None]          # "anthropic" | "openai" | "recraft" | "tripo" | "gemini" | None
    Params: ClassVar[type[BaseModel]]
    def cache_fields(self, p, inputs: list[Asset]) -> dict: ...   # model, prompt id/version/sha, schema hash, kit subset …
    def estimate(self, p) -> float: ...                           # USD, from providers/pricing.py
    def run(self, ctx: StepContext, p, inputs: list[Asset]) -> StepResult | Pending: ...
    def poll(self, ctx: StepContext, p, remote_ref: str) -> StepResult | Pending: ...   # remote steps only

class StepContext(Protocol):
    step: Step; project: Project | None
    def progress(self, frac: float, msg: str = "") -> None: ...
    def heartbeat(self) -> None: ...
    def check_cancel(self) -> None: ...                        # raises Cancelled
    def put_asset(self, data: bytes, ext: str, *, role: str, prov: Provenance, part_id: str | None = None,
                  status: str = "candidate") -> Asset: ...
    def set_remote_ref(self, ref: str, state: str = "submitted") -> None: ...   # commits at once (own transaction)
    def add_cost(self, entry: CostEntry) -> None: ...
    def record_checks(self, results: list[CheckResult]) -> None: ...
    def open_gate(self, gate: Gate) -> None: ...               # step -> WAITING_USER
    def spawn(self, steps: list[Step]) -> None: ...            # adds steps to this job (deps allowed)
    def call_ctx(self) -> CallCtx: ...
    def run_subprocess(self, argv: list[str], *, timeout_s: int) -> dict: ...   # records pid/create_time; JSON result file
```

| Step kind | Bible ID | Pool | Paid | What it does |
|---|---|---|---|---|
| `plan.reference` | L1 | api | ✓ | Reference analysis (skipped without a reference; cached per reference set) |
| `plan.taste` | L2 | api | ✓ | Taste-profile rebuild, only when ratings or decisions changed since the last one |
| `plan.planner` | L3 | api | ✓ | `PlanSet` of 3 specs |
| `plan.lint` | C1 | cpu | | Linter (HARD/SOFT) per spec and for the set |
| `plan.critic` | L4 | api | ✓ | Scores, one call per spec (anonymised) |
| `plan.pairwise` | L5 | api | ✓ | 3 pairs × 2 orders |
| `plan.revise` | L6 | api | ✓ | JSON patch from findings; ≤2 rounds per spec |
| `plan.select` | — | cpu | | Final order; guarantees 3 shown plans including exactly one wildcard |
| `img.draft` | I1–I10, R1, R2, G2 | api | ✓ | Compile + lint the prompt, call the provider, store n candidates |
| `img.gate_a` | Gate A | cpu | | Code checks + rung-1 auto-fixes per candidate |
| `img.gate_b` | L11 (+G1) | api | ✓ | Yes/no rules per surviving candidate (≤5 per call; the hard IP rules in their own call) |
| `img.rank` | — | cpu | | Chooses the best candidate; updates `LadderState.best_asset_sha` |
| `img.finalize` | I0 | api | ✓ | Sunburst edit of the chosen draft (skipped for Recraft SVGs and for Gate 1 drafts) |
| `img.recheck` | Gate A + A_DRIFT + L11 | cpu/api | ✓ | Re-runs every check on the final |
| `img.repair` | L10 → I11 | api | ✓ | Repair plan, masked edit, paste-back, ring check |
| `concept.assemble` | C2 | cpu | | 3072×1024 sheet, A_LEAK, clone proxy, then the L11 duo rules |
| `concept.lock` | C3 | api | ✓ | Finalize both characters, drift check, palette extraction, DNA card v1, per-duo style sheet, part crops |
| `face.assemble` | C4 | cpu | | Face-canvas layers, mirroring, code layers, line normalisation |
| `face.render` | C4 | cpu | | Pose sheet × 5 skin tones (head base) or 2D previews (no head base) |
| `face.check` | C4 Gate A/B | cpu/api | ✓ | Face-on-head checks and the face registry |
| `tripo.multiview` | T1 | api (remote) | ✓ | Upload the front, image-to-multiview, download the 4 views |
| `tripo.edit_view` | T2 | api (remote) | ✓ | Edit one view by text (once per set) |
| `mv.check` | A_VIEWS + L11 | cpu/api | ✓ | View consistency and direction |
| `clothing.compose` | compositor | cpu | | Layers → 585×559 Shirt/Pants + label maps + flat tiles |
| `clothing.check` | CHK-B01…B08, B11 | cpu | | Template validators |
| `colours.compose` | — | cpu | | Palette swatches, body/modesty flat preview |
| `hair.kit_match` | L9 | api | ✓ | Kit style + modules + adjustments |
| `hair.fit` | — | proc | | Assemble, fit and recolour the kit hair (trimesh; Blender only for FBX) |
| `polish.pack` | — | cpu | | Export a polish pack; opens a MANUAL_IMPORT gate |
| `manual.pack` | H1 | cpu | | Export a Tripo pack; opens a MANUAL_IMPORT gate |
| `mesh.import` | — | proc | | Parse a user file (GLB/FBX/ZIP), bake transforms, normalise |
| `tripo.model` | T3/T4 | api (remote) | ✓ | P2 multiview-to-model (or a fallback), one seed per step |
| `tripo.convert` | T5 | api (remote) | ✓ | Optional server-side reduce/convert |
| `mesh.repair` | bible §15.3 | proc | | Merge, weld copy, decimate, fix, texture, orient, scale, export |
| `mesh.validate` | A_MESH, CHK-M* | proc | | The accessory/hair file gate |
| `mesh.judge` | L11 (m3_*) | api | ✓ | Renders vs the approved views |
| `slab.build` | — | cpu | | Sticker / hair-clip slab from I6 art |
| `primitive.build` | — | cpu | | Rings, loops and straps |
| `head.texture` | C4 build | cpu | | LUT warp into the head UV (head base) |
| `head.check` | CHK-B09 | cpu/api | ✓ | The final head texture on the 5 FACS test poses |
| `template.finalize` | CHK-B* | cpu | | Final Shirt/Pants PNGs re-opened and validated |
| `body.compose` | CHK-B10 | cpu | | BodyColors, modesty texture, bundle validation (body kit) |
| `duo.render` | C5 | cpu | | 4 sides, three-quarter view, 5 face poses, phone strip, ID pass |
| `duo.checks` | CHK-D01…D09 | cpu | | Clone band, anchors, clipping, part vs concept, taste warnings |
| `duo.judge` | L12 | api | ✓ | Pairwise (both orders) or a single review |
| `duo.ip` | L13 | api | ✓ | Always-on final IP/appropriateness pass |
| `duo.similarity` | L14 | api | ✓ | Only when the toggle is on |
| `duo.second_opinion` | G1 | api | ✓ | Only when enabled, on ties or close calls |
| `duo.memory` | — | cpu | | After the Gate 3 pick: embeddings, registries, `duo_memory` |
| `export.build` / `export.validate` / `export.zip` | CHK-E* | cpu/proc | | Upload kit |
| `library.fabric` / `library.fold` | I7 / I8 | api | ✓ | Library builds (setup, or on demand) |
| `drill.render` | — | cpu | | Calibration variants (§3.8) |
| `gate.wait` | — | none | | A placeholder step that holds a gate open (WAITING_USER) |

### 8.3 Step state machine

```
             deps done              claim (lease 120 s)             handler returns
 PENDING ───────────────► READY ───────────────────────► RUNNING ─────────────────────► SUCCEEDED
                          │  ▲ cache hit ─────────────────────────────────────────────► SUCCEEDED (cached, $0)
                          │  │                             │  │  │
         budget exceeded ─┘  │ retryable error & attempt<max │  │  └─ submit remote (remote_ref committed) ─► WAITING_REMOTE
         -> WAITING_USER     └─────────────── backoff ◄──────┘  │                                  │  ▲
            (BUDGET gate)                                        │                     not_before  │  │ Pending
                                                                 │                     reached ────┘  │ (5 s, then 3->15 s x1.4)
                                                                 ├─ opens a gate ─► WAITING_USER      │
                                                                 └─ non-retryable ─► FAILED
 any non-terminal ── cancel ──► CANCELLED        FAILED ── user "Retry" ──► READY (attempts reset)
 SUCCEEDED ── upstream change replaces its output ──► SUPERSEDED (kept; outputs stay in the CAS until GC)
```

| From | Trigger | To | Notes |
|---|---|---|---|
| PENDING | all deps SUCCEEDED | READY | A dep FAILED or CANCELLED → this step CANCELLED (reason "upstream") |
| READY | cache hit, and every output is present in the CAS | SUCCEEDED | `cached=true`, cost 0, an event is emitted |
| READY | `paid`, and the estimate exceeds the remaining budget or `ask_above_usd` | WAITING_USER | BUDGET gate (§8.8) |
| READY | claimed by the scheduler | RUNNING | `lease_until = now + 120 s`, `lease_owner = instance id` |
| RUNNING | the handler returns a `StepResult` | SUCCEEDED | outputs linked, cache stored, cost committed |
| RUNNING | the handler called `set_remote_ref` and returned `Pending` | WAITING_REMOTE | `remote_ref` was committed before returning |
| WAITING_REMOTE | `not_before` reached | RUNNING (`poll`) | a poll never holds a thread longer than one HTTP call |
| RUNNING | `ProviderError(retryable=True)` and `attempt < max_attempts` | READY | `not_before = now + backoff(attempt) + jitter`; `retry_after_s` is honoured |
| RUNNING | `truncated` (first time) | READY | once, with `max_tokens` × 2 |
| RUNNING | `validation` (structured output) | SUCCEEDED with `result.validation_errors` | the plan loop sends them to L6 as findings (counts as a revision round) |
| RUNNING | `refusal`; `moderation` after one rule-based rewrite; `bad_request`; `permission`; `auth`; `not_found`; `recitation`; Tripo 2008 | FAILED | a user-visible reason; `billing` and `auth` also pause the queue |
| RUNNING | `submission_uncertain` | WAITING_REMOTE (`remote_state="submission_uncertain"`) | reconcile first; resubmit only if reconciliation proves nothing was created, and only after asking the user |
| RUNNING | the handler opens a gate | WAITING_USER | the gate decision resolves it |
| WAITING_USER | gate decision | SUCCEEDED, or new steps are spawned | §9 |
| any non-terminal | user or job cancel | CANCELLED | subprocess tree killed; stream closed. Remote Tripo tasks keep running (there is no cancel endpoint) and are still downloaded and billed |
| FAILED | user "Retry" | READY | attempts reset; the nonce is unchanged (use Reimagine for a new nonce) |

**Job state** is derived from its steps, in this order:
1. any READY / RUNNING / WAITING_REMOTE → RUNNING;
2. otherwise any WAITING_USER → WAITING_USER;
3. all SUCCEEDED or SUPERSEDED → SUCCEEDED;
4. a FAILED step with nothing left runnable around it → FAILED.

PAUSED is a user flag that the scheduler honours: no new claims; calls in flight finish.

**Stage order:** PLAN → Gate 1 → PARTS → Gate 2 → BUILD (+ MANUAL_MESH jobs) → DUO → Gate 3 → EXPORT. `Project.stage` follows the active job and gate.

### 8.4 Scheduler, leases, heartbeat, recovery

- **Claim** (one statement inside `BEGIN IMMEDIATE`):
  `UPDATE steps SET state='running', lease_until=?, lease_owner=? WHERE id=(SELECT id FROM steps WHERE state='ready' AND pool=? AND (not_before IS NULL OR not_before<=?) ORDER BY priority, created_at LIMIT 1) AND state='ready' RETURNING *`.
  The scheduler checks the per-provider semaphores and rate limiters before it claims.
- **Priority:** steps behind a tile the user is looking at get priority 10 (the UI sends `POST /api/focus {project_id, part_id}`); everything else 100; overnight batches 1000.
- **Heartbeat** thread: every 30 s it extends `lease_until` of every RUNNING step owned by this instance (long blocking SDK calls never call `progress()`).
- **Recovery at startup** (single instance, so leases are reclaimed only here; CHK-X01):
  - RUNNING with a `remote_ref` → WAITING_REMOTE (resume polling; never resubmit).
  - RUNNING without a `remote_ref` and `paid=true` → READY; the step's `reserved` ledger row for that attempt becomes `state="orphan"`, `basis="orphan"` (the call may have been billed), and the retry runs as a new attempt with its own reservation.
  - RUNNING and unpaid → READY.
  - Child processes recorded in `child_procs` (pid + create_time) are killed if still alive.
  - Open gates are announced again (`gate.opened` events), so the UI shows them.
- **Single instance:** `msvcrt.locking` on `DATA\run\instance.lock`. A second `start.bat` reads `run\server.json` and opens the browser on the running instance.
- **Keep awake:** `SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED)` from the scheduler thread while any step is READY, RUNNING or WAITING_REMOTE; cleared when the queue is empty.
- **Shutdown:** stop claiming; set the cancel flags; `executor.shutdown(wait=False, cancel_futures=True)`; WAL checkpoint; `os._exit(0)` after a 2-second grace period.

### 8.5 Content store and cache keys

```python
# duoskin/engine/cache.py
def pixel_sha(png_bytes: bytes) -> str:
    """Image identity for cache keys: decoded RGBA pixels + size, so metadata-only changes do not bust the cache."""
    im = Image.open(io.BytesIO(png_bytes)); im.load(); im = im.convert("RGBA")
    return hashlib.sha256(f"{im.width}x{im.height}".encode() + im.tobytes()).hexdigest()

def cache_key(step: Step, handler: StepHandler, p: BaseModel, inputs: list[Asset]) -> str:
    extra = handler.cache_fields(p, inputs)
    return sha256_of({
        "kind": handler.kind, "handler_version": handler.version,
        "provider": handler.provider,
        "model": extra.get("model"),                    # pinned snapshot, e.g. gpt-image-2.5-flare-2026-09-08
        "prompt_id": extra.get("prompt_id"), "prompt_version": extra.get("prompt_version"),
        "prompt_sha256": extra.get("prompt_sha256"),    # hash of the compiled text (slots filled)
        "schema_hash": extra.get("schema_hash"),        # Claude routes
        "params": p.model_dump(mode="json"),            # size, quality, background, n, seeds, controls, Tripo body
        "inputs": [a.pixel_sha or a.sha256 for a in inputs],   # ordered; images by pixel_sha
        "mask": extra.get("mask_pixel_sha"),
        "kit_subset_sha": extra.get("kit_subset_sha"),  # sha of only the kit entries this step reads
        "house_style_version": extra.get("house_style_version"),
        "style_guide_version": extra.get("style_guide_version"),
        "thresholds_version": extra.get("thresholds_version"),   # check steps only
        "rules_version": extra.get("rules_version"),             # Gate B steps only
        "capabilities": extra.get("capability_flags"),           # flags that change the request shape (mask_multi_ok, …)
        "nonce": step.nonce,                            # "" the first time; a new uuid on Reimagine
    })
```

- **What a key must include** (ENG-04, bible §2.10): step kind, handler version, provider, model snapshot, prompt id + version + compiled-text hash, schema hash, all params, the ordered input pixel hashes, the mask hash, the kit entries used, the house-style and style-guide versions, the threshold and rule versions for check steps, the request-shaping capability flags, and the nonce. A property test asserts that changing any one field changes the key (CHK-P07).
- **Reimagine = a new nonce.** Identical requests never pay twice, but the user can always force a fresh result.
- **Cached reuse across spec versions** is intended: if a change leaves a part's inputs byte-identical, its steps hit the cache and cost nothing.
- **CAS writes:** a temp file in the same folder, then `os.replace`, retried on `PermissionError` for up to 2 s (antivirus). Paths are `cas\<sha[:2]>\<sha>.<ext>`.
- **Raw bytes are archived untouched** (`raw_sha256`) before any normalisation, so C2PA or SynthID metadata survives in the archive. Derived files (normalised RGBA, stripped PNG chunks) are separate assets.
- **GC** (`python -m duoskin gc` or Settings → Storage): a dry-run report comes first. It deletes only assets with no link from an approval, a gate decision, an export, a registry, or a step newer than N days (default 30) (CHK-X03). Disk use per project is shown in Settings.

### 8.6 Errors, retries and user-facing messages

| ErrorKind | Engine action | UI |
|---|---|---|
| `rate_limit`, `overloaded`, `server`, `network` | READY with exponential backoff (1, 2, 4 … 32 s) plus jitter; the limiter is penalised on 429 | the step shows "waiting (rate limit)" |
| `timeout` | OpenAI: retry once and log the possible double billing; Anthropic streams: retry | same |
| `truncated` | once with 2× `max_tokens`, then FAILED | "The model ran out of room" |
| `validation` | returned to the plan loop or repair loop as findings | shown in the plan log |
| `schema_too_complex` | FAILED; Settings shows the route; the next kit rebuild turns the largest kit enum into `str` | "Planner schema too large for the kit; switched to checked strings" |
| `moderation` | one rule-based rewrite (bible §2.4d) as a new step; a second refusal → FAILED | the refused prompt and the rewrite are shown; nothing is retried unchanged |
| `refusal` | FAILED; `served_model` recorded (a fallback may have answered) | "Claude declined: <category>" |
| `recitation` | FAILED; the part goes to NEEDS_HUMAN with "originality failure: change it or plan again" | |
| `billing` | FAILED, and **pause the whole queue** | "Add credits for <provider>" |
| `auth` | FAILED; pause that provider | key dialog |
| `permission` | FAILED | provider-specific hint (OpenAI org verification) |
| `not_found` | FAILED | "Update the model ID in Settings" |
| `capability` | store the flag; retry once without the parameter | silent (logged) |
| `concurrency` (Tripo 2000) | READY after our own tasks finish; the Tripo slot count drops by 1 | |
| `submission_uncertain` | reconcile (§7.5) | "Checking whether Tripo received the job…" |
| `remote_failed` | Tripo 2018 → resubmit once; others → next seed once; then the user | |

Every error is stored as `StepError{kind, code, message, retryable, billed, provider_request_id, user_hint}`. Settings → "Diagnostics" builds a redacted zip of the logs, the failing steps' JSON and the doctor report.

### 8.7 Events and progress (SSE)

- Every event is inserted into `events` first (row id = SSE `id:`), then pushed to in-memory subscribers with `loop.call_soon_threadsafe`.
- `GET /api/events?after=<id>` is one SSE stream for all projects, with sse-starlette keep-alive pings every 15 s. The server replays rows with `id > after` before tailing.
- On page load the UI does **snapshot + tail**: `GET /api/state` (projects, jobs, a step summary, open gates, `max_event_id`), then opens the stream with `after=max_event_id`. `Last-Event-ID` covers automatic reconnects.
- Only the **leader tab** holds the EventSource. It is elected through the Web Locks API (`navigator.locks.request("duoskin-sse", …)`) and relays events to other tabs through `BroadcastChannel("duoskin")`, because browsers allow 6 HTTP/1.1 connections per host across all tabs. Fallback: `GET /api/events/poll?after=<id>` every 2 s.
- Progress events are throttled to ≤2 per second per step. The Jobs page shows an ETA per provider from recent durations and the rate and credit limits.

### 8.8 Budget and the BUDGET gate

- Every paid step has `estimate()` from `providers/pricing.py` (prices from `prices.json`). Before a paid step runs, `budget.reserve(project, est)` writes a `reserved` CostEntry. After the call, the entry is committed with the actual usage (`usage` tokens, Tripo `credits_consumed`, Recraft `credits`), or released.
- A BUDGET gate opens when `est > remaining` (cap default $15 per duo) or `est > ask_above_usd` (default $2). A Tripo P2 run at $1.10 does not ask; a large planner call may.
  - Gate actions: **Continue once**, **Raise cap to …** (`RAISE_CAP` with the new value), **Stop** (the step becomes FAILED with reason "budget").
- Tripo also requires `est_credits ≤ balance − frozen` (§7.5). A low balance shows a banner before the BUILD job starts.
- An optional daily cap across projects (Settings, default off) pauses the queue when reached.
- Cost display: every gate shows the project's spend so far; the top bar shows today's total across projects; the Costs page shows per-provider totals and the price-table date.

### 8.9 Fix ladder and technique ladder (`engine/ladder.py`)

The ladder runs **per part** (and per duo for rung 5). It follows the workflow summary and bible §19.

| Rung | Action | Who decides | Counts toward the 3-fix cap |
|---|---|---|---|
| 1 | Code auto-fix: palette re-snap, alpha clean-up, re-crop/re-pad, stroke normalise, re-place, edge decontamination | the `fix_hint` of the failing CheckResults | **No** [DECISION]: free and deterministic; runs at most once per candidate |
| 2 | Masked edit: L10 writes the repair plan, I11 edits, then paste-back + ring check (≤2 per asset) | L10 (`method == masked_edit`) | Yes |
| 3 | Regenerate the one asset: same template, new nonce, n raised to 6–8 | engine | Yes |
| 4 | The next technique in the asset's technique ladder (another template, Recraft vs GPT, code-parametric, the G2 backup model) | engine | Yes |
| 5 | Revise the plan (duo level only, e.g. the clone band or a same-world failure) | an L6/L7 proposal → **CHANGE_CONFIRM gate** (the user confirms, because approved parts may change) | Yes (duo counter) |
| 6 | Human review: a NEEDS_HUMAN tile with the best version so far and a short report | user | — |

```python
def next_action(part: Part, outcome: LoopOutcome, budget: Budget) -> LadderAction:
    hard_fails = [r for r in outcome.results if checks.policy.is_blocking(r) and not r.passed]
    if not hard_fails:
        return Done(best=outcome.best, warnings=outcome.soft_failures)   # soft checks never climb above rung 1
    if part.ladder.fixes_used >= T["ladder.max_fixes_per_part"] or budget.remaining < outcome.next_est:
        return Human(best=part.ladder.best_asset_sha, report=outcome.report)            # rung 6
    if outcome.code_fixable and not outcome.code_fix_tried:
        return CodeFix(outcome.fix_hints)                                               # rung 1 (free)
    if outcome.batch_pass_rate == 0:
        return NextTechnique(part.ladder.technique_index + 1)                          # 0% pass: skip rung 3 (U20)
    if outcome.near_miss and outcome.masked_repairs < 2:
        return MaskedEdit()                                                             # rung 2
    if not outcome.regenerated_once:
        return Regenerate(n=min(8, outcome.n * 2))                                     # rung 3
    return NextTechnique(part.ladder.technique_index + 1)                              # rung 4
```

- **Near miss:** exactly one failing HARD rule whose `location` is not `whole` and whose `fix_hint` is `masked_edit`.
- **Best version so far:** the most HARD passes, then the most SOFT passes, then code scores. It is always stored in `LadderState.best_asset_sha` and never deleted.
- A technique whose provider key is missing, or which has already failed twice for this asset, is skipped.
- The technique ladders per asset type are data (`data/rules.json → technique_ladders`), copied from bible §19.
- **Before a costly or irreversible step** (a Sunburst final, a Tripo P2 submit, an auto-approve), the gating VLM rules must pass a 3-vote majority; an auto-approve needs 3 of 3 (CHK-P12).

### 8.10 The asset loop (`pipeline/assetloop.py`)

Every generated 2D asset uses the same sub-graph, parameterised by an `AssetLoopSpec`:

```python
class AssetLoopSpec(Strict):
    part_id: PartId
    character: CharKey
    role: str                          # "iris_imgR", "print", "hair_front", "acc_front", "badge", "concept_a", …
    template_id: str                   # a bible id, e.g. "I2.print", "R1.face_part"
    slots: dict[str, Any]              # filled from the spec by pipeline code (never raw user text)
    images: list[Sha256]               # ordered references (Image 1 first); the guide first when masked
    mask: Sha256 | None
    n_drafts: int = 4                  # 6-8 for face parts and prints when the pass rate is below 50%
    finalize: bool = True              # False for Recraft SVGs and for Gate 1 drafts
    gate_a: list[str]                  # check ids (from the template front matter)
    gate_b_hard: list[str]; gate_b_soft: list[str]
    technique_ladder: str              # key into data/rules.json technique_ladders
    keep_alternatives: int = 0         # e.g. 2 for faces: show the top 2-3 at Gate 2
    stream: Literal["pipeline", "drill", "regression"] = "pipeline"
```

```
img.draft (n) ─► img.gate_a (per candidate, rung-1 fixes) ─► img.gate_b (L11 on the top 2 survivors; IP rules in a separate call)
   ─► img.rank ─► [0 survivors] ─► ladder.next_action()
               └► [best] ─► img.finalize (I0, same size/background) ─► img.recheck (Gate A + A_DRIFT + Gate B)
                              ├► drift fails twice ─► the tile shows draft and final side by side; the user picks (FM X4)
                              └► passes ─► part.board_assets[role] = final; the tile becomes READY
```

- **Pre-flight** for every provider call: compile + lint (CHK-P01), provider asserts (CHK-P02…P05), budget (CHK-P06), cache (CHK-P07).
- **Post-call:** CHK-P08…P10.
- Gate A always runs before Gate B. A judge "pass" never overrides a Gate A fail. Checks fail closed.

---

## 9. Gates, tile actions and invalidation

### 9.1 Gate lifecycle

- A handler opens a gate with `ctx.open_gate(gate)`; its step becomes WAITING_USER; the UI receives `gate.opened`.
- Decisions arrive at `POST /api/gates/{gate_id}/decisions` (§13). Each decision:
  - is idempotent by `client_decision_id` (a double-click never spawns twice);
  - is checked against the tile's `expected_version` (409 on conflict, ENG-11);
  - is stored as a `GateDecision`, then applied by `api/gates.py::apply_decision()`, which spawns steps or closes the gate.
- A gate is `decided` when every tile has a terminal decision (Gate 1: one approval, or a New plan; Gate 2: all tiles approved; Gate 3: pick + export).
- Every gate shows the money spent so far (`Gate.spent_usd_at_open` plus live `cost.added` events).
- A gate never shows SOFT warnings before the user's first choice on it (§9.9).

### 9.2 Gate 1: concept

**What the gate shows** (one card per plan, 3 cards; FAILURE_MODES CHK-G1-07):
- the 4-up sheet: A front, A back, B front, B back (the best checked Flare draft per character; the other drafts one click away);
- a **"Wildcard"** badge on exactly one card;
- the DNA card summary: WORLD fields (theme, pair structure, palette family, material family, detail level, anchors) and the CHARACTER fields of A and B side by side. The story is shown as a one-line caption marked "metadata, never drawn";
- the critic's level per criterion (compact) and the rank;
- the "Not buildable as drawn" panel (CON-04): features in the preview that the kits cannot build (for example a skirt flaring out of the leg boxes). The approved build follows the spec, not the painting;
- must-include coverage (§2 S18);
- the cost so far.

**Actions**

| Action | Scope | What runs |
|---|---|---|
| **Approve** | one plan card | C3 `concept.lock` (§10.3): redraw with Sunburst, drift check, palette extraction (snap-or-confirm), DNA card v1 locked, per-duo style sheet, part crops. Then the PARTS job starts. |
| **Reimagine** | one plan card | I1 again for both characters with a new nonce; drafts within pHash ≤6 of rejected drafts are dropped (A_PHASH). |
| **Select alternative** | one character slot | Pick another checked draft for that character. |
| **Change…** | one plan card (optionally a clicked figure) | L7 → CLARIFY or CHANGE_CONFIRM (§9.6) → patch → C1 → I1 only for the affected character(s). |
| **New plan** | the whole gate | L3 again, with the rejected specs and the user's reasons in `<avoid>`. The wildcard rule still holds. |

At approval, C3 shows a small **palette confirm** dialog when an extracted colour moved more than ΔE 10 from the planned colour: "use the colour from the picture" (the default, because the user approved a picture) or "keep the planned colour" (CHK-G1-08).

### 9.3 Gate 2: the part board

**Layout.** Two columns (A and B), with the shared DNA card and the palette at the top. Tiles per character: Colours & body, Face, Hair, one tile per accessory, one per print, Shirt, Pants (the workflow summary's page 4). Each tile shows **one part alone**:

| Tile | Shows |
|---|---|
| Hair | the front view on the bald-head guide + the 4 matching views (front, left, back, right); the kit match score when a kit exists; a "Views from GPT (lower reliability)" badge if I10 made them |
| Face | the face on the head in 4 expressions (neutral, blink, mouth open, happy) × 5 skin tones; the alternatives (top 2–3 assembled faces); a "2D preview — no head base" badge when there is no head base |
| Accessory | the front view + Tripo's 4 views + the on-body scale render (`guide_scale_<attachment>`: character outline, accessory at its planned size and attachment, the Classic box outline); for slabs: the badge art + the extruded slab preview; for primitives: the primitive preview |
| Print | the graphic alone + a 100 px readability preview |
| Shirt / Pants | flat front (torso FRONT + arm/leg F faces) and flat back (torso BACK + B faces), rendered by code from the compositor output, plus a 3D box preview; a "procedural folds" badge when no fold set exists |
| Colours & body | skin tone, modesty colour, palette swatches (main, second, shared, accent, hair) and the body front/back flat preview |

**Board-level actions:** "Approve all remaining" (it approves only READY tiles with no open HARD failure), "Back to concept" (returns to Gate 1 with the current spec; approved parts become STALE only where the new concept changes them).

When every tile is APPROVED, the BUILD job starts (§2 S10).

### 9.4 Gate 3: final pick

**Candidates.** The DUO job assembles 1–3 duo candidates **without new generation spend**: candidate 1 uses every part's approved choice. Extra candidates exist only when passing alternatives are already on hand (the second face of the top 2–3, a second passing Tripo seed, an alternative print final). Each candidate shows:
- A and B in front, back, left and right, and a three-quarter view;
- the phone strip (front and back at ~150 px, shown 2× nearest);
- the face in 5 poses;
- a live 3D viewer with both characters;
- the judge's notes (L12: pairwise in both orders, or a single review), the code facts, and the IP result (L13);
- the reference-similarity result when the toggle is on (L14), or the banner "Reference-similarity check is off".

**Actions:** **Pick** (a candidate), **Change one part** (L7 → redo only that part and its dependants → BUILD for that part → DUO again), **Export upload kit** (after a pick).

After the pick, `duo.memory` writes the cross-duo memory, registers the faces and prints (with `duo_seq`), and logs the DNA card.

### 9.5 Per-tile actions

| Gate / tile | Approve | Reimagine | Change… (typed) | Other |
|---|---|---|---|---|
| **Gate 1** plan card | C3 (redraw, palette lock, style sheet) | I1 for both characters, new nonce, rejected-pHash filter | L7 → patch → C1 → I1 for the affected character(s) | **New plan** → L3 with `<avoid>` + reasons; **Select alternative** draft |
| **Gate 2** hair | lock; L9 kit match runs in BUILD | I4 with a new nonce → T1 | L7 `image_fixes`: global → I4 edit with the fix + keep list; local → I11 on the front view (brush mask); a view-only problem → T2 (once per set) | **Make it myself on Tripo** → H1 pack (hair_custom only); **Back to concept** |
| **Gate 2** face | lock the parts; register them in the face registry at Gate 3 | only the failing or chosen part(s), new nonce (R1 or I3) | L7 → per-part fix: global edit of that part / R1 regenerate / a grammar-field patch | **Select alternative** among the top 2–3 assembled faces |
| **Gate 2** accessory | lock → T3 in BUILD (or the H1 pack in manual mode) | I5 (or I6) with a new nonce → T1 | L7: shape/colour → I5 edit; one view wrong → T2; size → spec patch (`size_class`); attachment → spec patch + re-lint | **Make it myself on Tripo** → H1 |
| **Gate 2** print | lock | I2 (or R2) with a new nonce | L7 → I2 global edit or regenerate; local → I11 with the brush mask | **Select alternative** |
| **Gate 2** shirt / pants | lock the compositor inputs (recipe, fabric, fold variant, placements) | re-roll the prints (I2/R2) or pick another fold variant | L7: recipe or cut change → patch → recomposite; colour → palette patch | brush a region → I11 on the print only |
| **Gate 2** colours & body | lock the palette and the modesty colour | — | L7 → palette patch → recolour the dependants (§9.8) | — |
| **Gate 3** candidate | **Pick** | — | L7 on one part → redo that part and its dependants → C5 → L12 | **Export upload kit** |

### 9.6 The change flow ("Change…" at any gate)

```
user text (+ optional clicked tile, + optional brush mask)
   └► ChangeRequest(status=interpreting) ─► L7 change interpreter (ChangePlan)
        ├─ needs_clarification != "" ─► CLARIFY gate (question shown; the answer is appended to the request) ─► L7 again
        └─ else ─► apply_patch() on a DRAFT copy of the spec (§6.4) ─► Pydantic ─► C1 lint
                   ├─ HARD lint fails ─► shown to the user with the reason; nothing applied ("rejected")
                   └─ ok ─► engine.deps.affected_parts() ─► InvalidationReport + cost estimate
                            └► CHANGE_CONFIRM gate: spec diff, DNA-card diff, parts to redo (REGENERATE / RECOMPOSE /
                               RECHECK), estimate, SOFT lint warnings
                                 ├─ Cancel ─► "cancelled"; nothing changes
                                 └─ Confirm ─► new SpecRecord (version+1) + DnaCard version; tiles set STALE/RECHECK;
                                              image fixes routed (below); old steps SUPERSEDED; status "applied"
```

**Routing of image fixes** (bible §10.5):
- `global_edit` → the part's own template, run as an edit with Image 1 = the current asset, the fix sentence as MUST 1, and the preserve list from `keep` plus the template's KEEP;
- `local_edit` → I11 with the user's brush mask (or a region-hint mask);
- `regenerate` → the part's template with the patched spec.

The fix sentence must pass the free-text lint and be ≤25 words (CHK-G0-11, PRM-12). User text never enters a prompt directly.

### 9.7 Approval hash (ENG-01)

```python
def approval_hash(part: Part, spec: DuoSpec, pins: VersionPins) -> str:
    return sha256_of({
        "part_id": part.id,
        "spec_slice": {path: resolve(spec, path) for path in part_dep_paths(part)},  # the part's own output-affecting paths
        "inputs": part_input_pixel_shas(part),        # concept crop, style sheet, guides, parent parts' outputs
        "outputs": sorted(part.board_assets.values()) + sorted(part.build_assets.values()),
        "prompts": part_prompt_ids_and_shas(part),
        "models": part_model_snapshots(part),
        "kit_subset_sha": part_kit_subset_sha(part),
        "house_style_version": pins.house_style_version,
    })
```

- Stored with the decision in `approvals` and in `Part.approval`.
- Recomputed at every BUILD step that consumes the part, at the DUO job and at export (CHK-D09, CHK-E02). A mismatch sets the tile to **STALE** ("re-approve"), and the step that noticed it opens the gate again.
- Pair-dependent checks (A_LEAK, face A-vs-B difference, garment cut, hair A ≠ B, accessory complement) are not in the hash. When the partner changes, these checks re-run (`RECHECK`); the approval survives if they pass (§2 S21).
- After the build, the hash also covers `build_assets`: a rebuilt mesh with a different file needs a new look at Gate 3, never a silent swap.

### 9.8 Dependency graph and invalidation (`engine/deps.py`)

`affected_parts(old, new, redo)` diffs the two specs path by path and applies the rules below (the parts' `deps: list[DepRule]` are generated from this table). `{c}` is the character that changed. The L7 `redo_parts` list is merged in (union). The report lists each part with its effect and the estimate.

| Changed spec path | Affected parts | Effect |
|---|---|---|
| `/palette/<id>/hex` | every part whose refs include `<id>`, **both characters** | shirt, pants, colours, hair recolour, face code layers: RECOMPOSE. Prints, badges and face parts (flat, palette-snapped art): recolour by palette-index remap ($0) → RECHECK. Accessory views and the textures of built meshes: palette remap ($0) → RECHECK; if the remap fails A_PALETTE (ΔE >12) → REGENERATE. |
| `/{c}/face/{iris_style,lash_style,brow_style,mouth_style}` | `{c}.face` | REGENERATE the affected AI part(s) only |
| `/{c}/face/{highlight_style,nose_style,cheek_mark,default_expression,eye_shape}` and the face `*_ref` fields | `{c}.face` | RECOMPOSE (code layers, re-placement, rig variant) |
| `/{c}/hair/{kit_style_id,fringe_id,back_id,description}` | `{c}.hair` | REGENERATE (I4 → T1); a built mesh is dropped |
| `/{c}/hair/{colour_ref,shadow_ref,highlight_ref}` | `{c}.hair` | RECOMPOSE (code recolour) |
| `/{c}/top/{recipe_id,sleeve,hem,neckline,front,block_layout,inner_recipe_id,fabric_id,*_ref,arm_extras}` | `{c}.shirt` | RECOMPOSE (a missing fabric tile spawns an I7 library step) |
| `/{c}/top/prints/<i>/motif` | `{c}.print.top.<i>`, then `{c}.shirt` | REGENERATE the print, RECOMPOSE the shirt |
| `/{c}/top/prints/<i>/{region,scale}` | `{c}.shirt` | RECOMPOSE |
| `/{c}/bottom/…` (incl. `shoes/style_id`, `legwear`) | `{c}.pants` | RECOMPOSE |
| `/{c}/bottom/shoes/motif`, `/{c}/bottom/prints/<i>/motif` | the matching print part, then `{c}.pants` | REGENERATE, then RECOMPOSE |
| `/{c}/accessories/<i>/{description,kind,material,build}` | `{c}.acc.<i>` | REGENERATE (I5/I6 → T1); a built mesh is dropped |
| `/{c}/accessories/<i>/{size_class,attachment,category}` | `{c}.acc.<i>` | RECHECK (scale tile, box fit) + re-fit in BUILD; no new art |
| `/{c}/dna/shape_language` | `{c}` parts whose templates route this field (bible §3.3): prints, face parts, hair front view, accessory front view, badge | REGENERATE those tiles only (PROPOSAL_DECISION: "redoes only the tiles that use that field") |
| `/{c}/dna/motif_object` | `{c}` accessory (I5) and badge (I6) tiles | REGENERATE |
| `/{c}/dna/{colour_plan,focal_location,accessory_style,energy}` | none (planner, critic and taste warnings only) | RECHECK (re-lint, SOFT warnings) |
| `/world/detail_level` | prints and face parts of **both** characters | REGENERATE |
| `/world/material_family` | none directly (fabric ids carry it) | RECHECK |
| `/world/{theme,story,pair_structure,structure_note,palette_family}`, `/shared_anchors`, `/contrasts` | none | RECHECK (re-lint; the structure profile can change SOFT warnings only) |
| `/{c}/body/skin_tone` | `{c}.colours`, `{c}.face` | RECOMPOSE colours; RECHECK the face tone sheet |
| `/{c}/body/modesty_ref` | `{c}.colours` | RECOMPOSE |
| `/combo`, `/is_wildcard`, `/{c}/presentation`, palette ids | — | not patchable after Gate 1 (New plan instead) |
| any change after Gate 3 opened | `duo` | STALE: the DUO job re-runs |

- **Pair re-checks:** after any change of `{c}`, the partner's parts run only the pair-dependent checks (§9.7) and keep their approval if they pass.
- **Built parts:** a REGENERATE or RECOMPOSE of a part that was already BUILT drops its build assets (they stay in the CAS until GC) and schedules the matching BUILD steps once the tile is approved again.
- **Cache:** a RECOMPOSE whose inputs end up byte-identical is a cache hit and the approval hash is unchanged, so the tile keeps its approval.

### 9.9 Warnings at gates (PROPOSAL_DECISION safeguards)

1. SOFT results are collected on each tile (`Part.open_warnings`) and on the gate, but the API withholds them.
2. The first decision on the gate sets `Gate.first_choice_at`. The API then releases **at most 2 warnings for the whole gate**, chosen by: visible (not auto-hidden, §3.1) → highest catch rate on rejected duos → severity.
3. The UI shows them as an "approve anyway?" step on the decision the user just made. While warnings are open, that approve decision is stored as **provisional**: its follow-up steps (C3, BUILD) do not start yet.
   - "Approve anyway" confirms the decision and logs each shown warning as a `warning_override` label.
   - "Go back" deletes the provisional decision; the tile returns to READY.
   
   Later decisions on the same gate show no further warnings unless new ones appear on a tile that was re-generated.
4. HARD failures are not warnings. A tile with a HARD failure is never READY; it is in the ladder or NEEDS_HUMAN.

### 9.10 Other gates

| Gate | Opens when | Shows | Actions |
|---|---|---|---|
| BUDGET | a paid step's estimate exceeds the remaining budget or `ask_above_usd` | the step, its estimate, the project spend, the cap | Continue once / Raise cap to … / Stop |
| MANUAL_IMPORT | a Tripo pack or polish pack was exported | the pack folder (Open folder button), the SETTINGS text, a drop zone, the inbox status | Import file (drop or pick) / Cancel (back to the API route or the tile) |
| HUMAN_REVIEW | ladder rung 6, or a rung-5 plan-revision proposal | the best version so far, the failing checks with evidence, what was tried, the cost | Accept best anyway (only if no HARD **Roblox/IP/stray-text** failure remains) / Change… / Back to concept |
| SETUP_APPROVAL | house-style exemplars or library tiles ready | the candidates | Approve / Reimagine |
| CLARIFY, CHANGE_CONFIRM | §9.6 | §9.6 | Answer / Confirm / Cancel |

---

## 10. Pipeline lanes

Each lane is a set of step handlers in `pipeline/*` plus the image, mesh and render helpers it calls. Everything sent to a model comes from the bible; this section defines the code around it.

### 10.0 Prompt compiler, templates and the router test

- **Templates** ship as `duoskin/prompts/<ID>.md` with the YAML front matter of bible §0.4 (`id`, `version`, `provider`, `route`, `size`, `background`, `images`, `mask`, `must_lines`, `dna_fields`, `slots`, `checks`). `prompts/registry.py` loads and validates them at startup; a template without a front-matter version refuses to load.
- **Compiler** (`prompts/compiler.py`):

```python
@dataclass(frozen=True)
class CompiledPrompt:
    template_id: str; template_version: int
    text: str                          # the exact text sent (stored in provenance)
    sha256: str
    must_lines: int; dna_fields: list[str]; character: CharKey | None
    images: list[str]                  # roles, in order (Image 1 first)
    provider_fields: dict              # e.g. Recraft controls.colors, Tripo prompts; never in the text

def compile(template_id: str, spec: DuoSpec, character: CharKey | None, slots: dict, ctx: CompileCtx) -> CompiledPrompt
def lint_prompt(cp: CompiledPrompt) -> list[CheckResult]      # CHK-P01; ASSERT: any failure raises before the call
```

  - Slots come only from spec fields, the human-written phrase maps (`data/phrases.json`), the kit catalogue (`prompt_phrase`), and linted model text (spec free-text, the L7 `fix_sentence`, the L10 `edit_prompt`). User text never becomes a slot (bible §2.3).
  - Colour names come from `data/colour_names.json` (nearest by ΔE2000, at most 3 per prompt). Hex codes never appear.
  - Empty slots remove their whole line; missing values raise (PRM-05). Joins are deterministic, so the same spec compiles to byte-identical text.
  - Style blocks (`HOUSE_STYLE_2D`, `HOUSE_STYLE_3D_INPUT`) are pasted verbatim from `data/style_guide.json`.
- **DNA router** (`prompts/dna_router.py`): the routing table of bible §3.3; at most 2 DNA fields per call, from the asset's own character only.
- **Router unit test** (`tests/unit/test_prompt_router.py`) compiles every template × 3 real fixture specs × both characters and fails the build if a prompt:
  - has more than 5 MUST lines, or more than 2 DNA fields, or a DNA field of the other character;
  - is longer than 1,500 characters without the STYLE block, or 2,200 in total;
  - has more than 10 nouns in EXCLUDE;
  - contains a hex code, a colour count, a ratio, the story text or a pair-structure word;
  - contains a banned word outside the places bible §2.4a allows, a backdrop word in a transparent template, or a priming word outside EXCLUDE (bible §2.4c).

### 10.1 Setup lane: house style, libraries, kits

| Item | Built by | When | Notes |
|---|---|---|---|
| House style sheet v1 | S0 bootstrap (bible §5.3): the user uploads 10–20 favourite skins (text analysis only, L1/L2) and rates ~50 samples; the app makes exemplars with I2, I3/R1, I4, I5 and I1; the user approves 6–8 at a SETUP_APPROVAL gate; code lays out the sheet | Setup wizard (once); a new version needs the 40-brief regression + variety guard | About $3–5 once [ESTIMATE] |
| Recraft face `style_id` | `recraft.create_style()` from 4–8 approved isolated face parts, rasterised to PNG | after the bootstrap; skipped without a Recraft key | Vector and raster style registries are kept apart |
| Fabric library | `library.fabric` = I7 (Flare → Sunburst medium) + code post-processing (desaturate, flatten lighting, 50% roll, seam repair with I11, roll back, moiré check at 128 px per torso face) | setup, or on demand when a spec names a missing `fabric_id` | Admission checks: seam energy ≤1.5×, alias energy ≤10%, flat lighting, greyscale |
| Fold / shading library | `library.fold` = I8 per recipe × panel + human curation (SETUP_APPROVAL) | setup | Missing sets fall back to `folds_procedural` (§2 S23) |
| Taste profile | L2 from frequency tables computed by code | whenever ratings or gate decisions change | Drill labels never feed it |
| Kits built outside the app (head base, hair kit, body base) | the user / a 3D artist, then "Add kit" on the Library page (copies into `DATA\kits\`, computes sha256, asks for `origin` and `license`) | once, then monthly growth | `hair\pair_iou.json` is recomputed on every hair-kit change (code renders silhouettes) |

### 10.2 Plan loop (PLAN job, until Gate 1)

```
plan.reference (L1, only with references; cached per reference set)
plan.taste (L2, only if the inputs changed)
plan.planner (L3) ─► plan.lint (C1, per spec + set) ─► plan.critic (L4 ×3) ─► plan.pairwise (L5 ×6)
   ─► plan.revise (L6, ≤2 rounds per spec: HARD lint findings + ValidationErrors + high-severity critic fixes)
   ─► plan.lint again ─► plan.select ─► concept drafts (§10.3)
```

- **L3 inputs** (bible §9.3): `<user_brief>`, `<combo>`, `<reference_analysis>`, `<taste_profile>`, `<recent_cards>` (last 5), `<recently_used>`, `<avoid>` (themes and anchor combinations of the last 30 duos; plans rejected in this session with the user's reasons), and, when set, the structure request and the must-include lines. Thinking summaries stream to the Plan page (`llm.thinking`).
- **C1 linter** (`pipeline/lint.py` + `checks/plan_rules.py`) implements bible §9.4, FAILURE_MODES §7.2 and this spec's §2 S18/S19 and §3:
  - HARD: Pydantic validity; the plan-set rules (3 specs, one wildcard, structure rule); anchors 2–3 visible from the front; ≥5 **measurable** contrasts on distinct axes with colour axes ≤2 (≤1 under same_club); combo vs presentations; kit ids and compatibility; face grammar ≥3 of 7; the garment-cut rule; ≥2 differing CHARACTER DNA fields; hair kit A ≠ B; slot → attachment pairs; category policy; size box; makeup routing (v1: `none`); the free-text lint; palette integrity and lash-vs-iris ΔE ≥10; accessories ≥4 per character; accessory complement (no shared (kind, category, motif)); exact registry reuse.
  - SOFT: structure-profile colour rules; anchor-colour ΔE; adjacent-area ΔE; restraint (colours, prints, 3 accessories); kit-hair pair IoU; detail-level range; accessory visible at phone size; nearest past DNA card.
- **Critic and ranking.** L4 per spec (anonymised, canonical JSON, code facts supplied; the wildcard gets "ignore taste_fit"), L5 in both orders (a disagreement counts as a tie). `plan.select` ranks by the HARD-clean state, then pairwise wins, then critic points (`structure_readable` and `accessory_pair_expresses` are ranking inputs only), then the novelty tie-break, and guarantees the wildcard is among the 3 shown.
- **Cheap critic mode** (Settings, bible §9.5): L4 only, no L5, when the budget is tight.
- **Dropped specs.** A spec that still fails HARD lint after 2 revision rounds is dropped, and the planner is asked for one replacement spec (at most once); otherwise Gate 1 shows 2 plans with the reason.

### 10.3 Concept (I1, C2) and concept lock (C3)

- **I1** runs once per character per plan (6 calls for 3 plans, in parallel within limits), on `guide_concept_char` (1536×1024, two 768×1024 slots, figures at 120 px/stud, colour-blocked from the recipe masks, a swatch strip, no text; `imaging/guides.py`). Draft settings: Flare `low` (setting: `medium`), n=4, opaque. A and B never see each other (bible D3).
- **Gate A/B on drafts:** A_SIZE, A_SIL_GUIDE (body boxes below the neckline ≥0.85), A_OCR, A_GLYPH, A_SWATCH (SOFT), A_PASTE, A_PHASH; then L11 on crops (cn_* rules, and the 4 hard `ip_*` rules in their own call).
- **C2** (`concept.assemble`): the 3072×1024 sheet; A_LEAK (HARD: partner `*_main` colours >3% of a figure); a clone proxy warning; L11 duo rules (dj_same_world SOFT, dj_not_clones HARD backstop, dj_anchor_visible SOFT, dj_no_leak HARD). A same-world failure runs the ladder rung "B with A as Image 3" (then A_LEAK and dj_no_leak again).
- **C3** (`concept.lock`) after approval:
  1. I0 FINALIZE with Sunburst `high` per character (Image 1 = the approved draft). A_DRIFT; one re-run on failure; after a second failure the user picks draft or redraw (default: the draft).
  2. Palette extraction per guide zone (k-means in CIELAB, skin excluded, shading bands merged), with the snap-or-confirm dialog (§9.2). Palette ids stay stable; `palette_source = concept_extracted`.
  3. DNA card v1, `locked=True`, written to `dna_cards`.
  4. The per-duo style sheet (both fronts cropped on white).
  5. Part crops per character: face, hair (front + back side by side), each accessory, each print region, the shoe band (bible §8.3). Upscaled with Lanczos to 512–1024 px, background kept flat `#F2F2F2` (bible U26).
  6. `plan_parts(spec)` creates the parts and the PARTS job.

### 10.4 Prints, motifs and badges (I2, R2, I6)

- **Routes:** I2 (GPT, default) with R2 (Recraft vector) as the A/B arm and ladder rung; shoe and bracelet motifs are I2 at small scale; badge art for `sticker_slab` and `hair_clip_slab` accessories is I6.
- **Size:** 1024² or 816×1632 for 1:2 limb panels (GPT); Recraft preset aspects only (1:1, 1:2).
- **Code after generation** (`imaging/checks.py`, `imaging/palette.py`):
  1. alpha checks (A_ALPHA), components, margin, OCR/glyph;
  2. clean-up in bible §2.5 order: binarise alpha at 128 → remove islands <0.2% → fill holes → decontaminate edge colours → palette-snap interiors only;
  3. A_STROKE at the **placed** size (the compositor's region scale), and the 100 px readability preview for the VLM rule;
  4. registry check (exact forever, near-duplicate within the window; §3.7).
- **Badge border** (I6): fill alpha holes → binarise → dilate N px → fill with the border colour, by code (never by the model).

#### 10.4.1 Transparency fallbacks

Sentinel background + palette-aware unmixing (`imaging/matte.py`: for each edge pixel choose the palette colour F and alpha a that minimise |P − (a·F + (1−a)·S)|) → Recraft `removeBackground` → local matting model in `DATA\models\matting\` (if present). The sentinel is the farthest of #00FF00, #FF00FF, #00FFFF, #0000FF from every palette colour (ΔE2000 ≥40).

#### 10.4.2 SVG path (`imaging/svg.py`)

1. Parse with `defusedxml`. Reject `<script>`, `<foreignObject>`, `<image>`, `<text>`, `<filter>`, `<mask>`, `<pattern>`, `<linearGradient>`, `<radialGradient>`, every `*href` attribute (an internal `<use href="#id">` is inlined instead), `on*` attributes, `opacity`/`fill-opacity`/`stroke-opacity` below 1, a `width="100%"` without a viewBox, and more than 300 paths (A_SVG). resvg reads local files by absolute path even without `resources_dir`, so this rejection is a security rule (SYS-03).
2. Make every colour explicit: push down inherited `<g fill>`, apply `<style>` classes and `style=""`, turn `currentColor` and a missing `fill` into black. Snap each colour to the palette (ΔE2000; reject a large fill more than 15 away).
3. Border pre-check: ≥95% of border pixels are the sentinel (A_SENTINEL).
4. Close seams: a same-colour stroke of 0.5 output px on every fill-only shape.
5. Render with `resvg_py.svg_to_bytes(svg_string=…, skip_system_fonts=True, resources_dir=<empty temp dir>)` at 4× through the root `width`/`height` (passing `width`+`height` to resvg does not stretch), then box-downsample in premultiplied alpha.
6. Remove the background with the **two-pass matte** (pass C: sentinel → black; pass M: sentinel → black, everything else → white; `alpha = M/255`, `colour = C/alpha`). A pixel chroma-key is never used.
7. ElementTree namespaces are registered (`""` = SVG, `"xlink"`) before writing.

The tested helper is bible Appendix A.

### 10.5 Clothing compositor and validators

**Inputs:** the character's `Top` or `Bottom` + `Shoes` + legwear + arm extras, the recipe from `builtin_kits/recipes/<id>.json` (bible §6.2), the fabric tile, the fold set (or procedural folds), the approved print finals, the palette.

```python
# duoskin/imaging/compositor.py
@dataclass
class Placement: part_id: str; region: str; box: tuple[int, int, int, int]; wrap: bool; scale: str

@dataclass
class ComposeResult:
    png: bytes                         # 585x559 RGBA 8-bit PNG, colour chunks stripped
    label_map: np.ndarray              # uint8 585x559: 0 transparent/skin, 1 fabric, 2 secondary block, 3 print, 4 trim,
                                       # 5 bracelet/glove, 6 shoes, 7 legwear
    layers: dict[str, bytes]           # per-layer PNGs at 4x (debug, tile drill-down)
    placements: list[Placement]
    flat_front: bytes; flat_back: bytes   # Gate 2 tile renders
    preview_boxes: bytes                  # 3D box preview (numpy raster)

def compose_template(kind: Literal["shirt", "pants"], char: Character, palette: list[Colour],
                     prints: dict[str, Asset], fabric: FabricTile, folds: FoldSet, kits: KitManifest) -> ComposeResult
```

**Layer order** (bible §6.2), all at 4× (2340×2236):
1. Base colour blocks from the recipe masks (by region, row ranges and shapes). Transparent where the recipe shows skin; torso rows 170–201 of a Shirt stay transparent for `waist_tucked` and `crop` tops so the waistband shows (CLO-09).
2. Fabric: the library tile gradient-mapped to the block colour; amplitude ≤ `fabric_amplitude_max_dL` (6 L\*).
3. Fold/shading overlay: multiply/screen from the fold set (or procedural bands), clamped to the recipe mask; panel edges feathered so neighbouring regions meet without a seam.
4. Seams, stitches and trims: code vectors (1 px at template scale, ΔL −20).
5. Prints: placed by code at region-relative anchors (scale small/medium/large); a print that must wrap across regions is marked `wrap` and split by the adjacency map; nothing is mirrored between regions.
6. Painted kit pieces: shoes (rows ≥446 to 482, plus D), legwear bands, bracelets/wristbands (arm rows 448–464, all 4 faces), gloves (rows 446–482 + D).

**Finishing:**
- box-downsample to 585×559;
- palette-snap interiors (anti-aliased edges untouched);
- fill the shared 2 px gaps 1 px per side with each owner's edge colour; bleed 2–4 px only on open sides and outer borders (CLO-03);
- keep details ≥2 px from rows 170, 418/419 and 467 and ≥5 px inside region edges (soft flags for prints);
- start from a **fully transparent canvas** (the official template PNG is only a guide overlay; CLO-18);
- write with `imaging/files.py::save_png_rgba8()` (strip gAMA/iCCP/sRGB chunks; re-open and verify).

**Validators** (`roblox/validators.py::validate_template`): CHK-B01 (exact region crops; golden round trip), B02 (585×559 RGBA 8-bit, no colour chunks), B03 (gap fill), B04 (fabric seam ΔE ≤6 mean, ≤15 max across the adjacency map), B05 (band placement; soft split-row flags), B06 (semi-alpha ≤0.5%; soft skin-in-clothing and waistband flags), B07 (layer-stack golden hash; prints inside regions unless `wrap`; no mirrored art), B08 (numbered-edge preview test in CI), B11 (outside-region pixels α=0; recipe skin α=0; no template label words by OCR). Region boxes come from `roblox/template_regions.json`, verified pixel for pixel against the official PNGs in `tests/golden/`.

**SOFT, from the label maps** (§3.4, §3.6): declared-vs-built colour plan; garment layout similarity A vs B (garment-vs-skin coverage + adjusted Rand index, colour-agnostic).

### 10.6 Face lane

**Parts generated per character** (bible §4.2): `iris_imgR`, `lash_upper_imgR`, `brow_imgR`, `mouth_closed`, `mouth_open` (R1 by default; I3 without a Recraft key or as the ladder rung; code-parametric as the last rung). `closed_lid_line_imgR`, highlights, lower lash ticks, nose, blush and the sclera are **code**. Makeup parts are disabled in v1.

**C4 face assembly** (`imaging/face_canvas.py`, `face.assemble`):
1. Load the face canvas: `kits/head_base/<variant>/face_canvas.json` when a head base exists, otherwise `builtin_kits/face_canvas_default.json` (a 2D layout on the cube head's front face).
2. Place the parts: sclera = the rig opening filled with `sclera_ref`; iris centred at the rig scale; lash arc-warped to the opening's top edge; brow on its baseline; mouths in their slots; the closed-lid line on the lid island.
3. Mirror `*_imgR` to `*_imgL` in image space (never for `smirk_side`). Assert the side naming (PRM-11).
4. Code layers: highlights at the **same image-space offset** in both eyes, white only (never mirrored, FACE-04); lower ticks; nose; blush (L\* ≤45, alpha ≤0.35); near-black low-alpha shading.
5. Normalise line widths to ≥2 px at the final texel density.
6. Face checks (§9.3 face tile, CHK Gate 2 face row): single-colour line features; catchlight sign equal in both eyes; feature pixels inside the landmark zones; line-to-skin ΔE ≥20 on 5 tones; shading darkens every tone; no hair or hairline on the head texture; face registry and A-vs-B face difference.

**With a head base** [DEPENDS: kit]:
- `head.texture`: warp the face canvas through `uv_lut.npz` into the head UV at 2–4× density in premultiplied RGBA, then downsample. Skin stays transparent; features 100% opaque. The LUT is keyed by `head_mesh.sha256`.
- `face.render`: render the 5 pose meshes (`poses\{neutral,blink,jaw_drop,happy,sad}.glb`, exported once from the rig with the FACS weights applied) with the new texture through the numpy rasteriser, at full size and at phone size, on 5 skin tones (`#F6DCC8`, `#E3B08E`, `#B9805A`, `#7B4B32`, `#3A2218` [CALIBRATE]) plus the spec tone.
- Checks: iris pixels = 0 in the blink render (also with EyesLookDown); stretch ≤1.5 per pose (`stretch.npz`); warp IoU ≥0.95; the neck seam ΔE ≤2 (CHK-B09).
- Output: `head_texture.png` (+ the head base FBX with the texture applied, exported through Blender when available).

**Without a head base** (§2 S11):
- The tile shows the face canvas on the mannequin's cube head front with 2D expression previews (neutral; blink = closed-lid layer; mouth open; happy = raised brows + open mouth) on the 5 tones, badge "2D preview — no head base".
- Output: a **face layer pack** (`face_layers\`: one PNG per part and code layer, `face_canvas.json`, the composite previews). The upload kit contains no Head item; the checklist says why. When a head base is added later, `head.texture` runs from the approved layers with no new AI cost (the approval stays valid if the output pixels equal the approved preview; otherwise the tile needs re-approval).

### 10.7 Hair lane

| Route | When | Steps |
|---|---|---|
| **Kit** (default) | `hair_route ∈ {auto, kit}` and the spec's `kit_style_id` is a kit style | PARTS: I4 front view on `guide_bald_head` (Image 3 = the kit style render) → recolour by luminance bands (code) → T1 multiview → `mv.check` → Gate 2. BUILD: L9 kit match (top 5 candidates by silhouette IoU) → `hair.fit` (assemble style + fringe + back modules in their common attachment frame, apply the discrete adjustments, recolour the band texture; trimesh/numpy, no Blender needed) → `polish.pack` → MANUAL_IMPORT gate (the user polishes, 30–60 min) → `mesh.import` → `mesh.repair` (target ≤3600 tris) → `mesh.validate` (Hair box, off-centre) → `mesh.judge` (m3_front_matches, m3_sides_match vs the approved views) |
| **Tripo backup** | `hair_custom`, or the hair kit is empty (bible D24), or `hair_route = tripo_api` | PARTS as above but without Image 3 → Gate 2. BUILD: `tripo.model` (P2, `face_limit` 3500, seeds 11 → 29 → 47 one at a time) → repair → validate → judge. The tile warns that spiky styles fuse into blobs (a SOFT depth <30% check routes flat cards back to the kit) |
| **Manual** | `hair_route = manual` or the user clicks "Make it myself" | The H1 Tripo pack (§11.3), or any GLB/FBX the user made in Blender → same import gate |

- **Polish pack** (`pipeline/polish.py`): `EXPORTS\PolishPacks\<duo>\<part_id>\` with `hair_fitted.fbx` (studs, +Z front, Y up), `hair_fitted.blend` when Blender is present, the 4 approved views, the head mannequin (`head_guide.fbx`), `POLISH.txt` (triangle target ≤3600, keep one mesh and one material, do not move the origin, save as `return\hair.fbx` or `.glb`). The inbox watcher picks up the `return\` file.
- **No Blender:** the pack contains `hair_fitted.glb` only, and the polish step becomes optional ("Skip polish" uses the fitted kit hair as is, after the same gate).
- Hair is never painted on the head texture (bible D25). Brows and lashes stay single-colour paint.

### 10.8 Accessories lane

| `build` | Gate 2 tile | BUILD |
|---|---|---|
| `tripo` | I5 front view (1024² transparent; symmetric frontal light) → upscale to 2048², re-pad to 80–85% → T1 multiview → `mv.check` (A_VIEWS; direction rules; T2 once per set; I10 as the flagged last resort) → scale render | §11: API (T3) or manual (H1) → repair → validate → judge |
| `sticker_slab` / `hair_clip_slab` | I6 badge art → code border → `slab.build` preview (extruded) → scale render | `slab.build`: alpha contour → simplify to the triangle budget → extrude to ≥0.08 stud with a bevelled rim → front texture = the art, **back on its own UV island** (un-mirrored art or a plain back) → texture 1024 opaque → CHK-M20 + the mesh gate |
| `code_primitive` | the primitive preview + scale render (no AI) | `primitive.build` (`mesh/primitives.py`: ring, loop, strap, bead; parametric, watertight, UV-mapped, recoloured by palette); may be unioned with a Tripo mesh through manifold3d (e.g. a keychain loop) |

- **Category and attachment** come from the spec and are re-checked at export (POL-05): items mostly above the neck are Hat or Face; a shoulder pet sits on a collar attachment by default; bags and keychains at the hip are Waist; there is no wrist type (bracelets are painted on the Shirt).
- **Scale render** (`guide_scale_<attachment>`, `render/sheets.py`): the character outline at a fixed stud scale, the accessory at its planned `size_class` converted to studs, the Classic box outline from the attachment (off-centre boxes for Hair, Back and Waist), attachment positions read from the mannequin in use (`mannequin_blocky.json`, FAILURE_MODES §4.2 X26).
- **Fit against hair.** Head-area accessories are fitted against the chosen hair, not a bald head (DUO-06): the BUILD step places them and runs the interpenetration check with the built hair.

### 10.9 Mesh import, repair and validation (both 3D modes)

All of this runs in `python -m duoskin.mesh.worker job.json` (proc pool, timeout 300 s), and writes a JSON result.

```python
# duoskin/mesh/worker.py  (job/result contracts)
class MeshJob(Strict):
    op: Literal["import", "repair", "validate", "render_views", "slab", "primitive", "fit_hair"]
    input_path: str; out_dir: str
    asset_type: Literal["Hat", "Hair", "Face", "Neck", "Shoulder", "Front", "Back", "Waist"]
    attachment: str                         # e.g. "RightCollarAttachment"
    target_studs: tuple[float, float, float]
    tris_target: int                        # 3800 accessories, 3600 hair
    texture_px: int                         # 1024 (512 for props of ~2 studs)
    approved_views: dict[str, str]          # view -> PNG path (for orientation and judging)
    mannequin: str                          # path of mannequin_blocky.json
    forward_axis: str                       # from the Studio calibration (settings.capabilities.studio.forward_axis)

class MeshResult(Strict):
    ok: bool; files: dict[str, str]          # "gltf", "bin", "png", "fbx", "glb_archive", "renders/*"
    facts: dict[str, Any]                    # tris, shells, bbox_studs, box_margins, surface_area, coplanar_frac,
                                             # centre_offset, normals_out, watertight, texture_px, orientation, mirrored
    checks: list[CheckResult]
```

**Repair steps** (bible §15.3), in order:
1. Parse by magic bytes; bake node transforms (`scene.to_geometry()`); read `extensionsRequired` (reject meshopt; Draco only with DracoPy; unknown → refuse and ask for a re-export). FBX and zipped OBJ go through headless Blender. Manual imports: triangulate quads and N-gons, strip armatures and skin weights.
2. Merge into 1 mesh, 1 material, 1 UV set with UVs in 0–1 (bake several materials into one atlas in Blender, when needed).
3. Weld a **copy** by position for the topology tests (UV seams split vertices).
4. Texture-aware quadric decimation (pymeshlab) to ≤3800 triangles (≤3600 hair). T5 convert is the optional fallback.
5. Watertight, non-manifold, zero-area and normal fixes; thickness ≥0.05 stud.
6. Keep closed shells (plush eyes); delete slivers and internal shells.
7. Texture: dilate edge colours into the UV gutters, then resize to 1024 (or 512); RGB PNG (alpha 255); not one flat colour; strip `COLOR_0`, emissive, metallic, roughness and normal maps; material OPAQUE, `metallicFactor` 0.
8. Orientation: the best of 24 axis rotations by silhouette IoU against the approved front (≥0.80, and better than the mirrored match by `acc.mirror_margin`). A mirrored best is **flagged, never auto-flipped**.
9. Scale to the planned studs; check every vertex inside the Classic box from the attachment (file frame, +Z front; FAILURE_MODES §4.2) and the Handle size; `AvatarPartScaleType = "Classic"`.
10. Export (`mesh/export.py`): `.gltf` + `.bin` + PNG with relative URIs (primary), `.fbx` with the texture embedded (backup; Blender, Apply Scalings = FBX Unit Scale, Path Mode Copy + Embed Textures), a `.glb` archive copy. All in studs, Y up, front +Z (§2 S6).

**Gate** (A_MESH; CHK-M01…M20): the full list is FAILURE_MODES §7.5; the renders for `mesh.judge` come from `render/raster.py` (front, left, back, right, top, three-quarter on grey), with facts (IoU per view, palette ΔE, thin-part IoU ≥0.6) passed to L11.

**Seeds and judging:** a failing T3 result moves to the next seed (≤3, one at a time) only after a 3-vote majority confirms the failure (CHK-P12); after the seeds, the technique ladder (bible §19: P2 image-to-model → P1 → H3.1 smart_low_poly → manual).

### 10.10 Colours and body

- **Colours tile** (`colours.compose`): swatches, the skin tone, the modesty colour (ΔE ≥10 from the skin), and the body front/back flat preview.
- **Body** [DEPENDS: kit]: with a body base, `body.compose` writes `BodyColors` and the modesty texture (both layers on every character, §2 S16; skin pixels transparent) and validates the bundle (CHK-B10: triangle budgets, `*_Geo` names, faces +Z, ≥50% fill per view, no SurfaceAppearance, classic-clothing UV layout within ±0.002 of R15_Block). Without a body base, the kit ships `body_colors.json` for the standard Block body and the checklist says the character uses the Roblox blocky body.

### 10.11 Renderer and the ID/label pass (`render/`)

- `render/raster.py`: a numpy z-buffer rasteriser (no GPU): orthographic and three-quarter cameras; beauty pass (flat albedo + one soft light band matching the house light, sRGB, no tone mapping) and **ID pass** (no anti-aliasing, nearest sampling, one flat id per part: skin, modesty, head, hair, each accessory, each sticker). Depth is kept for interpenetration checks.
- `render/avatar.py`: dresses the mannequin: the 5 composite canvases (torso 388×272, four limbs 264×284) baked from the templates, or the app's 1024×568 atlas with remapped UVs; layering body colour → Pants → Shirt on the torso; arms Shirt only; legs Pants only.
- The label maps from the compositor are rendered through the same UVs, so every beauty pixel has a garment label (fabric, block, print, trim, shoes …).
- `render/sheets.py`: fixed layouts: the Gate 3 duo sheet, the phone strip (area-downscaled to ~150 px, shown 2× nearest), the face pose sheet, the 5-tone sheet, the scale render, the mesh judge sheet. The render manifest is asserted (CHK-D01).
- The browser's three.js viewer (`web/components/viewer3d.js`) is for interaction only; it follows the same UV rules (`texture.colorSpace = SRGBColorSpace`, `NoToneMapping`, `flipY=false` for GLTF textures, default `flipY` for template `CanvasTexture`s). A "Studio look" preset matches the calibration screenshot from FM-T1. Checks never depend on the browser.

### 10.12 Duo loop and checks (DUO job, until Gate 3)

```
duo.render (both characters, every candidate) ─► duo.checks ─► duo.ip (L13) ─► duo.similarity (L14, only if ON)
   ─► duo.judge (L12: pairs in both orders, or a single review) ─► [close call and G1 on] duo.second_opinion ─► Gate 3
```

| Check | Kind | Notes |
|---|---|---|
| Roblox validators on every file (A_TEMPLATE, A_MESH, box fits, category rules) | HARD | re-run on the final files |
| Clone band lower edge: 4-side DreamSim A vs B ≥0.30 [CALIBRATE] (CHK-D02) | HARD | degraded mode without `dreamsim.onnx`: pHash ≥ threshold + palette overlap ≤50% + A-vs-B spec distance, labelled "degraded" on the tile. A fail goes to rung 5 (CHANGE_CONFIRM) |
| Interpenetration ≤0.5% per view (CHK-D04); seams (edge continuity on the renders) | HARD | |
| Each part vs its concept crop: palette ΔE ≤12 (CHK-D05) | HARD | consistency class |
| Always-on IP: OCR, brand words, L13 (`unsure` blocks until the user resolves it with a logged note) (CHK-D06) | HARD | |
| Reference similarity L14 + DreamSim/pHash vs the references (only when ON) | HARD on `near_copy`; `specific_element` is a warning | banner when OFF |
| Approval hashes still valid for both characters (CHK-D09) | HARD | |
| Colour anchors at phone size (ΔE ≤6, area ≥1%); motif anchors (VLM) | SOFT (§2 S19) | |
| Strangers upper edge / same-world style | SOFT | |
| Phone-size top colours; A-vs-B hair/accessory silhouette; garment layout similarity; nearest past duo; eyes or brows hidden by hair | SOFT (≤2 shown, after the first pick) | |

- L12 criteria and schemas: bible §17.2. A `blocking_defects` item becomes a note on the tile it names; it blocks only if a code check confirms it.
- Ranking = pairwise wins (both orders; disagreement = tie) → novelty tie-break (nearest past duo).

### 10.13 Export kit (`pipeline/export.py`)

**Folder** (`EXPORTS\Kits\<duo_slug>_<yyyymmdd-hhmm>\`, ASCII slug names, short paths):

```
README.txt                     ASCII: what is in the kit, the order of the Studio tests, the fees, the banners
CHECKLIST.html                 the interactive checklist (also on the Export page; tick state mirrored in the DB)
checklist.json                 item-type rows with their steps and tick state
manifest.json                  every item: type, category, attachment, target studs, files, sha256, tris, texture px
provenance.json                §Appendix B
dna_card.json  spec.json       the final DNA card and spec (story included as metadata)
A_<name>\
  classic\shirt.png  classic\pants.png                585x559 RGBA 8-bit PNG
  head\head_texture.png  head\head.fbx                (head base present)   or   head\face_layers\*.png + face_canvas.json
  hair\hair.gltf  hair\hair.bin  hair\hair.png  hair\hair.fbx
  accessories\<category>_<kind>\acc.gltf .bin .png  acc.fbx  fit.json  accessory_wrapper.luau (UNTESTED until FM-T5)
  body\body_colors.json  (body\body.fbx + modesty texture when a body base exists)
  renders\front.png back.png left.png right.png three_quarter.png phone.png face_poses.png
B_<name>\ …  (same layout)
duo\duo_sheet.png  duo\phone_strip.png
studio\property_check.luau     command-bar script: checks the Accessory tree (Handle properties, names, DoubleSided, …)
studio\calibration_arrow.gltf  only until the user has recorded the forward-axis calibration
<duo_slug>.zip                 the same content zipped
```

**Export gate** (CHK-E01…E09, all before the folder is written):
- manifest complete: exactly one approved asset per part, present in the CAS with a matching sha256, no `mock`/`placeholder` source and no `drill`/`regression` stream;
- every approval hash valid; every asset carries the project's pinned versions;
- provenance complete (snapshots, prompts, params, input/output hashes, request ids, seeds, cost, licence, reference-toggle state, SynthID note);
- every exported file re-opened and re-validated (classic PNGs; meshes through the mesh gate; stud extents ±1% of the manifest; `.gltf` URIs are sibling file names only);
- category consistent with attachment and position;
- secret scan over the folder and the zip (regexes + exact stored key values);
- path/slug linter;
- kit lineage: every kit asset has an allowed `origin` and a sha256; any `license: unknown` in an uploadable item's lineage (for example a head base built on `BlockyCharacter.fbx`, POL-07) adds a banner and a manual confirmation line;
- a Tripo free-plan licence (`tripo_free_public_ccby_noncommercial`) adds a banner "not for sale" (selling is out of scope, but the flag is kept).

**Checklist** (`roblox/checklist.py`, from the item-type table; EXP-01…EXP-10):

| Item type | Channel and fee | Steps in the checklist |
|---|---|---|
| Classic Shirt / Pants | Creator Dashboard (browser) → Avatar Items → Classics → Upload Asset; 80 Robux per submission, not refunded; ID verification | 1. Free Studio test: Avatar tab → Block Avatar rig; insert Shirt/Pants; set the template (tick "Studio test passed"). 2. Upload (locked until 1 is ticked). |
| Hair / accessories | Studio: 3D Importer → Accessory Fitting Tool → UGC Validation tool → Save to Roblox; 80 Robux each (500 with emissive, which we never use) | Importer settings: "Upload to Roblox" **off** while testing; Scale Unit = Studs; World Forward = Front; World Up = Top; Merge Meshes off; Rig Scale = Default (Classic). AFT: category, attachment and the numeric offset from `fit.json`. MeshPart: Material Plastic, Transparency 0, VertexColor 1,1,1, no extra objects. Run the free UGC Validation tool; tick "Studio test passed" before the upload line unlocks. |
| Head (head base present) | Studio (Avatar Setup or manual); 80 Robux | Studio's head validator (17 FACS poses, blink, mouth, happy, sad) first; the licence confirmation line when the base's lineage has `license: unknown`. |
| Body | Studio; 80 Robux | Only with a body base: the modesty layers, the bundle contents (only hair/brow/lash accessories), the body validator. |
| All | — | Confirm the Gate 3 renders, the file list and the category per item before any upload (items cannot be edited after upload). The creator-docs commit and validator defaults are printed; the R6 caveat is noted; the "reference-similarity check was off" banner when applicable. |

**provenance.json** — see Appendix B for the schema.

---

## 11. 3D: Tripo API mode vs manual mode

### 11.1 The rule

The app **always** produces the images and views needed for 3D and **always** accepts a model back. The API and manual paths share everything except the step that makes the mesh (T3 vs the user on Tripo's website). Both end in the same `mesh.import → mesh.repair → mesh.validate → mesh.judge` gate (FAILURE_MODES CHK-M01…M20).

| | API mode (`mesh_mode = api`) | Manual mode (`mesh_mode = manual`) |
|---|---|---|
| Views | T1 multiview from the approved front (+T2 once per set; I10 last resort) | the same Gate-2-approved views (T1 needs the Tripo key; without it, I10 views flagged lower-reliability) |
| Mesh | T3 P2 multiview-to-model, `face_limit` per kind, seeds one at a time | the user runs Tripo Studio → Smart Mesh → P2.0 with the exported pack |
| Cost | 110 credits per seed (~$1.10), expected ~190 credits per accessory incl. multiview [ESTIMATE] | the user's web-plan credits (a separate wallet) |
| Licence | `tripo_api_private_commercial` | asked in the import wizard: paid → `tripo_paid_private_commercial`; free → `tripo_free_public_ccby_noncommercial` (public, CC BY 4.0 label, no commercial rights) |
| Waits | WAITING_REMOTE (polling) | MANUAL_IMPORT gate |

`mesh_mode = ask` (the default) asks once per project when the BUILD job starts, with the estimate for the API route.

### 11.2 API mode sequence

```
approved views (4 PNG, 2048², same height, common ground line; Tripo's own views never re-cropped)
  ─► tripo.upload (POST /files, just before submit; free)
  ─► budget.reserve(110 credits, requires <= balance - frozen)
  ─► tripo.model: POST /generation/multiview-to-model (P2Params.body()); remote_ref committed
  ─► WAITING_REMOTE: poll 5 s, then 3→15 s ×1.4; 20 min soft timeout = "slow", keep polling
  ─► success: download model_url + rendered_image_url AT ONCE (host allowlist, no auth, 150 MB, magic bytes)
  ─► mesh.import ─► mesh.repair ─► mesh.validate ─► mesh.judge (3-vote majority before the next seed)
  ─► pass: part BUILT   |   fail: next seed (29, then 47), then the technique ladder, then the user
```

### 11.3 The Tripo pack (manual mode; also offered on every hair and accessory tile)

Written by `manual.pack` to `EXPORTS\TripoPacks\<duo_slug>\<asset_id>\` (§2 S5). "Make it myself on Tripo" is available on the tile before and after the build.

| File | Contents |
|---|---|
| `00_FRONT_single.png` | the approved front, 2048², for single-image mode |
| `01_FRONT.png`, `02_LEFT_subject-left.png`, `03_BACK.png`, `04_RIGHT_subject-right.png` | the 4 approved views, 2048², same scale, flattened on `#FFFFFF` (or `#D9D9D9` when the object's edge is near-white, ΔE <10 to white) |
| `views_sheet.png` | a contact sheet with code-drawn arrows showing which way the object's front faces in each side view (no text on the views themselves) |
| `SETTINGS.txt` | bible H1 template (ASCII): the free-plan warning first (public, CC BY 4.0 label, no commercial rights; do not upload unreleased designs on the free plan), then the steps: Smart Mesh → P2.0 → Multi-view if offered (else single image) → the slot mapping ("Left" = the object's own left) → Triangles, face limit about {face_limit}, texture on (standard 2K), PBR off, no compression → download the GLB at once (free history ~1 day) → drop it on the tile or save it into `{inbox_path}` |
| `asset.json` | `asset_id`, `project_id`, `part_id`, kind, category, attachment, target studs, `face_limit`, SHA-256 of every view, the pack version |

Before the first pack of a project, the UI shows the free-plan warning as a confirm dialog (ACC-10).

### 11.4 Import flow (manual files, polish returns, any user-made mesh)

1. **Arrival.** The user drops a file on the tile, or saves it into `TripoPacks\inbox\` (or a polish pack's `return\`). The inbox watcher:
   - ignores `.crdownload`, `.part`, `.tmp`;
   - waits until the size has been stable for 2 polls and the file opens exclusively;
   - hashes it and records it in `inbox`.
2. **Assignment.** A file named `<asset_id>.glb` (or found inside that asset's pack folder) is assigned automatically; anything else appears in an "Unassigned imports" list where the user picks the tile (CHK-M16: `asset_id` must match).
3. **Import wizard** (`api/imports.py`, a MANUAL_IMPORT gate decision):
   - "Which Tripo plan made this file?" free / paid / not Tripo (made by me in Blender or elsewhere: `license = user_made`);
   - an optional Tripo task link (stored in provenance);
   - for free-plan files: the licence flag and the warning.
4. **Upload safety** (SYS-04): 50 MB cap; type from content (magic bytes); `.zip` extracted safely (no absolute or `..` paths; ≤500 MB total, ≤200 files); FBX, zipped OBJ and `.blend` converted by headless Blender in the mesh worker (without Blender: "please export as GLB" message); meshes are only parsed in the subprocess.
5. **Optional T5.** With a Tripo key, the user may send an over-budget or odd file to Tripo `/models/convert` (on a file_token if `tripo.convert_on_file_token`, else import → convert; 5–10 credits; always PNG textures at 1024, `export_vertex_colors: false`).
6. **Gate.** The same repair, validation and judging as the API path (§10.9). A failing import returns the tile to MANUAL_IMPORT with the reasons (for example "mirrored model: check the Left/Right slots"), and the user can try again or switch to the API route.

---

## 12. UI pages

The UI is plain ES modules in `web/`, one view per page, routed by `router.js` (hash routes). Every page gets live updates from the leader tab's SSE stream. A top bar shows: the project stage bar, "spent so far" for the open project, today's total, the queue status (running / waiting on you / paused), a doctor warning if any, and the Quit button.

| Page (route) | Purpose | Main elements |
|---|---|---|
| **Home** (`#/`) | Start and resume | Project cards (stage, combo, spent, waiting-on-you badge); "New duo"; the demo-mode banner; the doctor status; links to Library, Calibration and Learning |
| **Setup wizard** (`#/setup`) | First run | 1. Keys (Anthropic, OpenAI, Tripo required; Recraft recommended; Gemini/fal optional) with "Test key". 2. `doctor`. 3. Blender (optional, auto-detected). 4. Kits status (head base, hair kit, body base, fabric/fold libraries), each with "what happens without it". 5. Taste sources (10–20 favourites) + ~50 ratings. 6. House-style bootstrap (S0, SETUP_APPROVAL). 7. Test day (optional, the §19 probes with their costs) |
| **Settings** (`#/settings`) | §14 | Tabs: Keys, Budgets, Models & versions (defaults and candidates; promotion needs the regression), Providers (real/mock/disabled, rate limits, capability flags), 3D (mesh mode, Blender path, Studio calibration), Checks (reference-similarity default, sparkle-star switch, demoted checks, OCR models), Folders, Storage (disk use, GC dry-run), Diagnostics, Optional components |
| **New duo / Brief** (`#/new`) | Brief form | Combo picker (B+B, G+G, B+G, G+B with "first = character A"); brief text; "Pair structure" dropdown (let the planner choose / a structure); must-include lines (≤5); references upload (≤4) with the "use as mood image" switch (off; turning it on shows the banner recommending the similarity check) and the **"Check similarity to my reference"** switch (off); budget cap; mesh mode; start button with the plan-loop estimate |
| **Plan** (`#/p/<id>/plan`) | Watch the plan loop | Step timeline (L1 … L6), the planner's thinking summary, lint results per spec (HARD and, after Gate 1's first choice, SOFT), critic levels, revisions with their patches, the concept-draft progress |
| **Gate 1** (`#/p/<id>/gate1`) | §9.2 | 3 plan cards with the 4-up sheets, "Wildcard" badge, DNA card (world + A/B side by side), "Not buildable as drawn" panel, must-include coverage, per-card Approve / Reimagine / Change…, the gate's New plan; draft alternatives per character; the palette-confirm dialog after Approve |
| **Part board** (`#/p/<id>/board`) | §9.3 (Gate 2) | Two columns (A, B); the DNA card and palette on top; tiles with state, badges and cost; tile drawer: large views, alternatives, the check summary (HARD results and evidence; SOFT after the first choice), the provenance summary, Approve / Reimagine / Change… / Make it myself on Tripo, the brush-mask editor (`brushmask.js`) for local edits, the 3D viewer for views; "Approve all remaining", "Back to concept" |
| **Build** (`#/p/<id>/build`) | BUILD progress | Per part: step list, Tripo task state ("slow" shown as normal), manual-import waits with pack links and the drop zone, polish packs, validation results (tris, box margins, orientation, mirrored flag) |
| **Gate 3** (`#/p/<id>/gate3`) | §9.4 | Candidates side by side: 4 sides + three-quarter, phone strip, face poses, the 3D viewer with both characters; judge notes, code facts, IP result, similarity result or banner; Pick, Change one part, Export |
| **Export** (`#/p/<id>/export`) | §10.13 | The kit file tree, "Open folder", the checklist with tick boxes (upload lines locked until "Studio test passed" is ticked), banners, the provenance viewer |
| **Jobs** (`#/jobs`) | Queue | Steps by state and provider, ETA, pause/resume per project, retry a FAILED step, cancel, the error with its user hint |
| **Costs** (`#/costs`) | Ledger | Per project and provider; estimate vs actual; orphan rows; Tripo balance; the price-table date |
| **Library** (`#/library`) | Kits and registries | Hair styles (with silhouettes and the pair-IoU matrix), head base variants, body base, fabrics, fold sets, house-style versions, Recraft style ids; "Add kit" (origin + licence required); the registries (face/print, window, "listed" flags); the kit-growth panel |
| **Calibration** (`#/calibration`) | §3.8 | Locked until ≥5 approved duos. Drill sessions (blind; clone / real duo / strangers + like/dislike; timer 10–15 min; ≤5 variants per base); judge calibration sets (FM-T9: known negatives, 30–50 labels per hard rule, flip test); label counts by source (drill share ≤25%) |
| **Learning** (`#/learning`) | §3.9 | The weekly report: per-check flag rate on approved vs catch rate on rejected, auto-hidden and demoted checks, the hard-reject rate on approved duos, structure use, wildcard pick rate, Gate 1 first-try approval, nearest-duo distances, registry reject rate, cost per duo; regression runs and the variety guard; version promotion; the #4 revisit triggers |

**UI rules**
- Every image is a CAS URL (`/cas/<sha>.<ext>`), so it is never stale.
- Tiles never show SOFT warnings before the gate's first choice (§9.9).
- Every "Change…" box says "describe the visible result; the app will show you the changes before anything runs".
- The DNA card component (`dnacard.js`) highlights which fields each tile uses (from the routing table), so the user sees what a change to a field would redo.
- Accessibility: every action is a real `<button>`; colours are never the only signal (badges have text).
- No inline `style=""` attributes (CSP); styles via classes or the CSSOM.

---

## 13. HTTP API

All routes are under `/api`, JSON in and out, served only on `127.0.0.1`. **Every** POST/PUT/PATCH/DELETE requires the `X-DuoSkin-Token` header (the per-launch token from the `<meta name="duoskin-token">` tag of `index.html`) and, when an `Origin` header is present, `Origin == http://127.0.0.1:<port>`. `TrustedHostMiddleware(allowed_hosts=["127.0.0.1", "localhost"])` rejects other hosts with 400; a bad Origin or token gets 403 (`{"error": "bad_token"}` makes the UI reload). Optimistic-lock conflicts return 409 with the current object.

| Method | Route | Body / params | Returns |
|---|---|---|---|
| GET | `/api/health` | — | `{ok, version, instance_id, demo}` (no DB access beyond a ping; p95 <1 s, CHK-X05) |
| GET | `/api/state` | — | snapshot: projects, jobs, step summary, open gates, `max_event_id` |
| GET | `/api/events` | `?after=<id>` (SSE; `Last-Event-ID` wins on reconnect) | event stream |
| GET | `/api/events/poll` | `?after=<id>` | `{events: [...], max_event_id}` |
| GET | `/api/doctor` | — | last doctor report |
| POST | `/api/doctor/run` | — | starts doctor; result as `doctor.result` event |
| GET | `/api/settings` | — | `Settings` (keys masked) |
| PUT | `/api/settings` | `SettingsPatch` | `Settings` |
| GET | `/api/keys` | — | `{provider: KeyStatus}` (set / not set / masked tail / last test result) |
| PUT | `/api/keys/{provider}` | `{value}` (≤1280 chars) | `KeyStatus` (value never echoed) |
| DELETE | `/api/keys/{provider}` | — | `KeyStatus` |
| POST | `/api/keys/{provider}/test` | — | probe result (Recraft test costs $0.08 and says so) |
| GET | `/api/projects` | — | list of `ProjectSummary` |
| POST | `/api/projects` | `{name, combo, brief, structure_request, must_include[], settings}` | `Project` |
| GET | `/api/projects/{id}` | — | `Project` + parts + current spec + DNA card |
| PATCH | `/api/projects/{id}` | `{expected_version, name?, settings?}` (settings only before PLAN) | `Project` |
| POST | `/api/projects/{id}/references` | multipart image (≤50 MB; type sniffed) + `role` | `ReferenceImage` |
| POST | `/api/projects/{id}/plan` | — | `Job` (PLAN) |
| POST | `/api/projects/{id}/pause` / `/resume` | — | `Project` |
| POST | `/api/projects/{id}/archive` | — | `Project` (never deletes approved or exported assets) |
| GET | `/api/projects/{id}/specs` | — | `SpecRecord[]` with DNA cards and lint |
| GET | `/api/specs/{spec_id}/diff/{other_id}` | — | JSON diff + DNA-card diff |
| GET | `/api/gates/{gate_id}` | — | `Gate` (SOFT warnings withheld until the first choice) |
| POST | `/api/gates/{gate_id}/decisions` | `GateDecisionIn` | `GateDecision` + released warnings (≤2) |
| POST | `/api/gates/{gate_id}/decisions/{decision_id}/confirm` | `{override_warnings: [ids]}` | `GateDecision` (provisional → final) |
| DELETE | `/api/gates/{gate_id}/decisions/{decision_id}` | — | 204 (only a provisional decision) |
| GET | `/api/changes/{change_id}` | — | `ChangeRequest` (+ plan, diff, invalidation, estimate) |
| GET | `/api/projects/{id}/parts/{part_id}` | — | `Part` + asset links + checks + provenance summary |
| POST | `/api/projects/{id}/parts/{part_id}/tripo-pack` | — | pack folder path (opens the MANUAL_IMPORT gate) |
| POST | `/api/uploads/mask` | multipart PNG (the brush mask; RGBA; sized to the tile image) | `{sha}` |
| POST | `/api/imports` | multipart mesh file + `{project_id, part_id?}` | inbox entry |
| POST | `/api/imports/{inbox_id}/assign` | `{project_id, part_id, tripo_plan: free / paid / not_tripo, task_link?}` | starts `mesh.import` |
| GET | `/api/inbox` | — | unassigned and pending imports |
| GET | `/api/jobs` | `?project_id=&state=` | jobs with step summaries |
| POST | `/api/steps/{step_id}/retry` | — | `Step` (FAILED → READY) |
| POST | `/api/jobs/{job_id}/cancel` | — | `Job` |
| POST | `/api/focus` | `{project_id, part_id}` | 204 (raises priority for that tile) |
| GET | `/api/costs` | `?project_id=&from=&to=` | ledger rows + totals |
| POST | `/api/projects/{id}/export` | — | `Job` (EXPORT) |
| GET | `/api/exports/{project_id}` | — | kit manifest, checklist, banners |
| PATCH | `/api/exports/{project_id}/checklist` | `{item_id, step_id, ticked, expected_version}` | checklist (upload lines unlock only after "Studio test passed") |
| POST | `/api/os/open-folder` | `{kind: export / tripo_pack / polish_pack / inbox / logs, id}` | 204; `os.startfile` only on an app-created directory, never a file |
| GET | `/api/library` | — | kit manifest summary, availability flags, registries summary |
| POST | `/api/library/kits` | `{folder_path, kind, origin, license}` | copies and registers a kit; rebuilds the manifest |
| POST | `/api/library/rebuild-manifest` | — | manifest sha + schema smoke-test result |
| GET | `/api/calibration/session` | `?kind=drill or ?kind=judge` | next blind items |
| POST | `/api/calibration/labels` | `{item_id, label, like?}` | 204 |
| GET | `/api/learning/report` | `?week=` | the weekly report |
| POST | `/api/regression/run` | `{candidate_versions}` | `Job` (REGRESSION) |
| POST | `/api/versions/promote` | `{role, version}` | refused unless the latest regression passed the variety guard |
| POST | `/api/diagnostics` | — | redacted zip path |
| POST | `/api/shutdown` | — | 202, then graceful shutdown |
| GET | `/cas/{sha}.{ext}` | — | the file; `immutable` cache, `nosniff`, `Content-Security-Policy: sandbox` |

`/api/docs` exists only in dev mode (it loads Swagger UI from a CDN, which the CSP blocks otherwise).

**Security headers** (`security.py`) on every HTML response: `Content-Security-Policy: default-src 'self'; img-src 'self' blob: data:; script-src 'self' 'sha256-<importmap>'; style-src 'self'; object-src 'none'; frame-ancestors 'none'`, `X-Content-Type-Options: nosniff`, `Referrer-Policy: no-referrer`; `index.html` is `Cache-Control: no-store`. No CORS middleware. No auth cookie.

---

## 14. Settings

### 14.1 Settings model (`models/settings.py`, stored in `DATA\settings.json`; keys never stored here)

```python
class ProviderMode(StrEnum): REAL = "real"; MOCK = "mock"; DISABLED = "disabled"

class ModelPins(Strict):                  # defaults for NEW projects; projects pin their own copy (VersionPins)
    planner: str = "claude-opus-5"; critic: str = "claude-opus-5"; judge: str = "claude-opus-5"
    checker: str = "claude-sonnet-5"
    image_draft: str = "gpt-image-2.5-flare-2026-09-08"
    image_final: str = "gpt-image-2.5-sunburst-2026-09-08"
    recraft_face: str = "recraftv4_styles_vector"; recraft_bootstrap: str = "recraftv4_1_utility_vector"
    recraft_print: str = "recraftv4_1_vector"
    tripo_mesh: str = "P2-20260801"; tripo_fallback_p1: str = "P1-20260311"; tripo_fallback_h31: str = "v3.1-20260211"
    gemini_judge: str = "gemini-3.8-flash"; gemini_image: str = "gemini-3.1-flash-image"
    candidates: dict[str, str] = {}        # role -> candidate snapshot awaiting regression + variety guard

class Budgets(Strict):
    per_duo_usd: float = 15.0              # thresholds: budget.per_duo_usd
    ask_above_usd: float = 2.0
    daily_cap_usd: float | None = None
    tripo_credit_floor: int = 200          # banner when balance - frozen falls below

class ProviderSettings(Strict):
    modes: dict[str, ProviderMode] = {p: ProviderMode.REAL for p in ["anthropic", "openai", "recraft", "tripo", "gemini"]} \
        | {"fal": ProviderMode.DISABLED}
    openai_ipm: int = 5                    # images per minute (Tier 1 reportedly 5 [UNVERIFIED]); drafts use n <= min(ipm, 4)
    anthropic_concurrency: int = 3; openai_concurrency: int = 3; recraft_concurrency: int = 2; tripo_slots: int = 2
    gemini_key_billed: bool = False        # CHK-P11: private images go to Gemini only when true

class ThreeD(Strict):
    default_mesh_mode: Literal["api", "manual", "ask"] = "ask"
    blender_path: str | None = None        # auto-detected on first run (§15.3); validated with --version
    blender_tested_versions: list[str] = ["4.2", "4.5", "5.0", "5.1", "5.2"]
    studio_forward_axis: Literal["unknown", "+Z", "-Z"] = "unknown"   # from the calibration arrow import (FM-T5)
    hair_default_route: Literal["auto", "kit", "tripo_api", "manual"] = "auto"
    build_start_per_tile: bool = False

class ChecksSettings(Strict):
    reference_similarity_default: bool = False   # requirement 7: default OFF; per project in ProjectSettings
    sparkle_star_allowed: bool = True             # bible D26 [UNVERIFIED policy]
    ratio_check: bool = True                      # §3.4 optional declared-vs-built
    check_overrides: dict[str, Literal["soft"]] = {}   # demoted checks (§3.1); never roblox/ip/stray_text/security
    cheap_critic_mode: bool = False
    gemini_second_opinion: bool = False
    face_route_ab: Literal["off", "r1_vs_i3"] = "off"

class Paths(Strict):                             # expanded with os.path.expandvars at use; validated by the path linter
    exports_root: str = r"%USERPROFILE%\DuoSkin Exports"
    tripo_inbox: str = r"%USERPROFILE%\DuoSkin Exports\TripoPacks\inbox"

class Settings(Strict):
    schema_version: int = 1
    port: int = 8765                              # sticky; the last bound port is written back
    demo_mode: bool = False                       # all providers mock + banner; export blocked
    dev_mode: bool = False                        # /api/docs, verbose logs (never ANTHROPIC_LOG=debug)
    models: ModelPins = ModelPins()
    budgets: Budgets = Budgets()
    providers: ProviderSettings = ProviderSettings()
    three_d: ThreeD = ThreeD()
    checks: ChecksSettings = ChecksSettings()
    paths: Paths = Paths()
    planner_structure_lru_hint: bool = False      # §3.3: suggested only after collapse
    capabilities: dict[str, bool | str] = {}      # §7.1 flags from probes and errors
    telemetry: Literal["off"] = "off"             # nothing leaves the PC except provider calls
```

- `settings.json` is written atomically (temp + `os.replace`). Unknown fields are refused (`extra="forbid"`); a migration step upgrades old versions.
- Model pins: a new default only after the 40-brief regression passes the variety guard (§3.9). Aliases (`-latest`) are rejected by a validator.

### 14.2 Keys (`keystore.py`)

- **Store:** `keyring` with service `DuoSkinStudio` and one entry per provider (`anthropic`, `openai`, `tripo`, `recraft`, `gemini`, `fal`), in Windows Credential Manager.
- **Fallback:** if keyring fails, a DPAPI-encrypted file `DATA\secrets.dpapi` (`CryptProtectData` via ctypes, current-user scope).
- **Dev override:** environment variables `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `TRIPO_API_KEY`, `RECRAFT_API_TOKEN`, `GEMINI_API_KEY`, `FAL_KEY` win over both (Settings shows "from environment").
- **Limits:** a secret longer than 1280 characters is refused (Credential Manager stores 2560 bytes as UTF-16).
- **Hygiene:** keys are read only in the backend; the API returns masked tails only; `RedactFilter` masks `sk-…`, `tsk_…`, `Bearer …`, `x-api-key` and the exact stored values in every log line (CHK-S12); keys never enter provenance, exports or diagnostics (CHK-E06 scans for them).

### 14.3 Which settings are per project

`ProjectSettings` (§6.3) copies the defaults at project creation: budget cap, ask threshold, reference-similarity check (default **off**), mood image (default off), mesh mode, hair route, Gemini second opinion, concept quality, build-per-tile. The reference-similarity check can be switched on or off at any time before export; switching it on runs L14 on the current concept or duo renders at once.

---

## 15. Install, launch and `doctor`

All `.bat` files are **ASCII only with CRLF line endings** (`.gitattributes`: `*.bat text eol=crlf`; `build_portable.py` writes CRLF; CI checks both, CHK-X06). They never call `Activate.ps1` (blocked by the PowerShell execution policy on many PCs) and never call a bare `python` (often the Store stub).

### 15.1 `tools\probe_python.py`

```python
import sys, sysconfig
ok = (sysconfig.get_platform() == "win-amd64"                 # x64 only (no 32-bit, no ARM64 build)
      and not sysconfig.get_config_var("Py_GIL_DISABLED")     # no free-threaded 3.14t
      and "windowsapps" not in sys.base_prefix.lower()        # not the Store stub or an install-manager alias
      and sys.version_info[:2] in ((3, 14), (3, 13)))
sys.exit(0 if ok else 1)
```

### 15.2 `setup.bat`

```bat
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
if not defined PY if exist "tools\uv.exe" (
  "tools\uv.exe" python install 3.14 --install-dir ".python"
  for /d %%D in (".python\cpython-3.14*-windows-x86_64-none") do call :try "%%~D\python.exe"
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

rem 4) Check the machine. Exit code 2 = installed, but doctor found problems (shown in the app).
".venv\Scripts\python.exe" -m duoskin doctor --setup
if errorlevel 3 goto :fail
if errorlevel 2 echo Setup finished with warnings. Open DuoSkin Studio to see what needs fixing.
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
```

Notes:
- `py -V:X` does not auto-install a missing version once any runtime exists, so the install manager path calls `py install 3.14` explicitly.
- The app folder must not contain spaces in the `.python` path (the default `C:\DuoSkin\app` has none); `README.txt` says so.
- `--no-deps` is safe because the lock is complete; the antlr4 wheel comes from `wheelhouse\` (its hash is in the lock).
- The duoskin package itself is not installed: `-m duoskin` runs it from the app root (`cd /d "%~dp0"` puts the root on `sys.path`). CHK-S13 guards against stdlib-named files there.

### 15.3 `start.bat` and `doctor.bat`

```bat
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
```

```bat
@echo off
rem DuoSkin Studio - doctor. ASCII only, CRLF line endings.
cd /d "%~dp0"
set "PYTHONUTF8=1"
".venv\Scripts\python.exe" -m duoskin doctor
pause
```

### 15.4 CLI (`python -m duoskin …`)

| Command | What it does |
|---|---|
| `run [--open-browser] [--port N]` | Starts the app (sequence below) |
| `selfcheck` | Imports the package and its pure-Python dependencies; exit 0/1 (used by `start.bat`) |
| `doctor [--setup]` | Runs CHK-S01…S13 (FAILURE_MODES §7.0), prints a report, writes it to the DB; exit 0 ok, 2 warnings/HARD failures that block paid features, 3 broken install |
| `reset-leases` | Recovery helper (the same logic as startup recovery, §8.4) |
| `export-diagnostics` | Redacted zip of logs, failing steps and the doctor report |
| `gc [--dry-run]` | Storage clean-up (§8.5) |
| `regression [--candidate role=version …]` | The 40-brief regression + variety guard (§3.9) |
| `build-kit-manifest` | Rebuilds `DATA\kits\manifest.json` and runs the schema smoke test |
| `calibrate-report` | Prints the weekly report (§3.9) |

**`duoskin run` sequence:**
1. Very first lines of `__main__`: set `OMP_NUM_THREADS=1`, `OPENBLAS_NUM_THREADS=1`; `faulthandler.enable()`; `truststore.inject_into_ssl()`; `os.add_dll_directory(sys.prefix)` (+ `Scripts`) when `msvcp140.dll` is there; the mimetypes fix.
2. Take the single-instance lock (`msvcrt.locking` on `run\instance.lock`). If another copy holds it, read `run\server.json`, open the browser there, and exit 0.
3. Load settings; migrate and back up the DB; `PRAGMA integrity_check`; load the kit manifest (rebuild if kit folders changed); recovery (§8.4).
4. Bind the socket ourselves: `socket.socket()`, `SO_EXCLUSIVEADDRUSE`, `bind(("127.0.0.1", port))` trying the saved port, then 8765, then 8766–8799 (WinError 10013/10048 → next port). Write `run\server.json` with the port and instance id; save the port as sticky.
5. Generate the per-launch token; compute the import-map hash for the CSP.
6. Start uvicorn: `uvicorn.Server(Config(app, log_config=None, access_log=False, timeout_graceful_shutdown=2)).run(sockets=[sock])` in the main thread; the scheduler, heartbeat and inbox threads start in the app lifespan.
7. Turn QuickEdit off; register the console control handler.
8. Open `http://127.0.0.1:<port>/` only after `/api/health` answers (Edge `--app=` mode is an option in Settings).
9. Run `doctor` in the background, followed by the free provider probes of bible §8.1.6 (Claude `models.retrieve` + `allowed_fallback_models` + the schema smoke test; Tripo balance and a 1×1 upload; Gemini `models.get`). The paid probes run only on request: the OpenAI 1024² Flare-low probe (~$0.006) once per new key, the Recraft probe ($0.08) only on "Test key". Results arrive as a `doctor.result` event and become capability flags.

**Blender detection** (`mesh/blender.py`), in order: the Settings path → `DUOSKIN_BLENDER` → `shutil.which("blender")` → the registry `.blend` open command (swap `blender-launcher.exe` for `blender.exe` in the same folder; key name [UNVERIFIED]) → `C:\Program Files\Blender Foundation\Blender *\blender.exe` (highest version) → Steam `common\Blender`. Confirmed with `blender.exe --version`; the version must be in `blender_tested_versions`; CHK-S09 renders a colour chart (view transform Standard) and requires ΔE2000 <2.

### 15.5 Packaging

- **Phase 1 (v1):** a zip with the app folder, `setup.bat`, `start.bat`, `doctor.bat`, `wheelhouse\`, `requirements\`, `README.txt`. The README says: extract to `C:\DuoSkin\app`; open the zip's Properties and tick **Unblock** first; run `start.bat`.
- **Phase 2:** a portable zip built by Windows CI: a runtime extracted with `py install --target=runtime 3.14` (or a uv-managed Python), with packages installed into **that runtime's own site-packages**. No venv is shipped (`pyvenv.cfg` and `Scripts\*.exe` hold absolute paths).
- PyInstaller is **not** used (antivirus false positives, onnxruntime DLL loading in frozen apps, one-file unpacking to %TEMP%).

---

## 16. Mock mode and demo mode

- **Per provider:** Settings → Providers sets each provider to `real`, `mock` or `disabled`. Environment override: `DUOSKIN_PROVIDERS=mock` (all) or `DUOSKIN_PROVIDERS=anthropic:real,tripo:mock,…`.
- **Demo mode** (Settings → "Demo mode (no keys)", also offered by the setup wizard when no key is set):
  - every provider is mock;
  - a persistent banner: "DEMO — nothing here can be exported";
  - built-in demo kits (`builtin_kits/demo/`: 3 code-made hair styles, a demo fabric tile, procedural folds, the default 2D face canvas) stand in for missing user kits, marked `origin=code_generated`, `stream=pipeline`, and never enter the registries;
  - the whole flow runs end to end: brief → plan → Gate 1 → part board → build (API and manual import, using the sample GLBs in `providers/fixtures/meshes/`) → duo → Gate 3 → export **preview** (the export gate stops at CHK-E01 "mock sources").
- **Tests** may set `export.allow_mock_for_tests=True` (a pytest fixture only; not reachable from Settings); the kit is then written to a temp folder with `MOCK` in every file name.
- **Fault injection** for tests and demos: `DUOSKIN_MOCK_FAULTS="openai:moderation_blocked@first,tripo:2008@seed29,anthropic:refusal@L13,anthropic:truncated@L3,anthropic:schema_invalid@L3,openai:size_drift@I2,tripo:read_timeout_after_send@T3,recraft:429@first,tripo:1007@first,tripo:2000@first,openai:timeout@I4"`. Each fault raises the same `ProviderError` the real adapter would.
- **Determinism:** mock output depends only on the canonical request, so re-running a demo gives the same pictures, and the cache works exactly as with real providers.

---

## 17. Test plan

### 17.1 Infrastructure

- pytest 9.1.1; `tests/_alias_httpx.py` loaded as an early plugin (`addopts = "-p tests._alias_httpx"`), so tools that patch `httpx` also see SDK traffic.
- **No outbound sockets** in the test run (a socket guard fixture); every provider test asserts that its `httpx2.MockTransport` handler was hit (SYS-22).
- SDK clients get `http_client=anthropic.DefaultHttpxClient(transport=httpx2.MockTransport(h))` (verified for anthropic) and the openai equivalent.
- Fixtures: the official Shirt/Pants template PNGs (golden), 3 real fixture specs (bible Appendix D and two more), plan sets, SVGs (clean and hostile), meshes (clean, over-budget, meshopt-compressed, mirrored, multi-material, FBX with quads and an armature), images for every Gate A rule (pass and fail), a numbered-edge test texture.

### 17.2 Unit tests (must exist in v1)

| Area | Tests |
|---|---|
| Prompts | `test_prompt_router` (§10.0; the PROPOSAL_DECISION router test); `test_banned_terms` (the colour dictionary has no banned term; NFKC; word boundaries); `test_compiler_deterministic`; `test_no_missing_slots`; `test_planner_prompt_has_no_example` |
| Schemas | `test_schema_lock` (models vs `SCHEMAS.lock`); `test_llm_schemas_no_optional_no_union` (0 `anyOf`, 0 optional after `transform_schema` + all-required); enum lowercasing |
| Plan lint (C1) | one pass and one fail fixture per HARD and SOFT rule; `test_no_outline_overlap_rule`; `test_structure_profiles` (colour rules keyed by structure; anchor ΔE only for colour anchors); `test_structure_request` (explicit → all 3; auto → all different or all equal; 2+1 fails); `test_dna_character_diff`; `test_garment_cut`; `test_no_novelty_lints` |
| Check policy | `test_policy_classes` (taste/colour_distance/restraint/novelty are SOFT); `test_ladder_soft_never_climbs`; `test_fail_closed` (a checker exception gives `ran=False, passed=False`); warning release after the first choice; auto-hide at >25% overrides; demotion never touches roblox/ip/stray_text/security |
| Template | region crops exact (128², 128×64, 64×128, 64²); inclusive-box helper; gap fill 1 px per side; open-side bleed; adjacency map with the numbered-edge texture; transparent base; chunk stripping; label map classes |
| Images | `valid_size`; `make_mask` polarity and size; `paste_back` + ring check; A_ALPHA (real alpha, painted checkerboard FFT, haze, halo); A_COMPONENTS; A_MARGIN; A_PALETTE snap; A_STROKE at placed size; A_DRIFT; pixel_sha stability across metadata changes; EXIF/ICC/tRNS/I;16 normalisation |
| SVG | sanitizer rejects every banned element and `href`; missing fill → black; seam closing; two-pass matte edge error ≤6/255; resvg never reads a local file |
| Face | image-space side naming; mirroring never flips highlights; smirk not mirrored; single-colour line features; blink iris pixels = 0 (with a head-base fixture); tone-sheet contrast; no hair on the head texture |
| Mesh | magic-byte sniffing; `extensionsRequired` rejection; welded-copy watertight test; decimation keeps UVs; 24-rotation orientation with a mirrored fixture (flagged, not flipped); box fit with off-centre Hair/Back/Waist offsets in the file frame; texture opaque RGB; `COLOR_0`/emissive stripping; slab thickness and un-mirrored back; exporter round trip with an asymmetric "F" (CHK-M19) |
| Engine | every state-machine transition (§8.3); cache-key property test (CHK-P07); approval hash changes when any output-affecting input changes and not otherwise; dependency table rows (§9.8) incl. DNA-field routing; budget reserve/commit/release; ledger uniqueness per (step, attempt, operation); SQLite stress: 8 threads × 1,000 claims → 0 lock errors, 0 double claims (CHK-X07) |
| Providers | Claude: refusal, max_tokens, model_context_window_exceeded, schema-violating JSON, missing text block, served-model logging, Opus via beta + fallbacks, Sonnet via plain stream; OpenAI: size drift, moderation rewrite once, unknown parameter → capability flag, `usage=None`, mask with several images when `mask_multi_ok=false`; Recraft: styles model without `style_id` refused, 429 bucket, field-name fallback; Tripo: named view inputs, `P2Params.body()` never emits `compress`/`quad:true`/`pbr:true`, 1007/2000/2008/2018 handling, `submission_uncertain` reconciliation, storage host allowlist without auth, URL expiry re-GET; Gemini: empty text = FAIL, thought parts skipped, JPEG sniffing |
| Security | bad Host → 400; bad Origin / missing token → 403; secrets never echoed; `index.html` no-store; CAS sandbox header; `.js` MIME; zip-slip rejected; RedactFilter with the exact key values |
| Calibration | drill isolation (`stream="drill"` never read by registries, taste profile or critic inputs); drill share ≤25%; the tuner sets bad-tail bounds only; variety-guard arithmetic |

### 17.3 Golden and snapshot tests

- The 18 template regions against the official PNGs, pixel for pixel.
- Compiled prompts for every template × 3 fixture specs (reviewed snapshots; a change needs a template version bump).
- Compositor output for each recipe with fixed inputs (layer-stack hash, CHK-B07).
- Mock-mode renders of a fixed duo (render manifest and pixel hashes).

### 17.4 End-to-end tests in mock mode (`tests/e2e_mock/`)

| Scenario | Asserts |
|---|---|
| Happy path, B+G, open brief | 3 plans with 3 structures and one wildcard; Gate 1 approve → C3 palette lock + DNA v1; every Gate 2 tile READY; approve all → BUILD (API mode) → DUO → Gate 3 pick → export (test flag) with a complete provenance and checklist |
| Named structure (`same_club`) | all 3 specs use it; same-club profile (shared main allowed, ≥4 non-colour contrasts) |
| Manual 3D | pack written with the 7 files; a sample GLB dropped into the inbox (with a `.crdownload` first) → import wizard → same mesh gate; free-plan licence flag and banner |
| Change at Gate 2 | "make B's jacket teal" → L7 palette patch → CHANGE_CONFIRM shows the diff and RECOMPOSE parts → only those tiles change; partner parts RECHECK and keep approval |
| DNA field change | `shape_language` of A changed → only A's routed tiles REGENERATE |
| Reimagine | new nonce, no cache hit, drafts near rejected ones dropped |
| Budget | a low cap opens a BUDGET gate before the step that crosses it; Stop fails the step with "budget" |
| Crash recovery | kill during a Tripo poll → restart → WAITING_REMOTE resumes, no second submit (CHK-X01) |
| Faults | each injected fault ends in its documented state (§8.6), with the right UI hint and no double charge |
| Warnings | no SOFT warning in any gate response before the first choice; at most 2 after it; overrides logged |
| Without Recraft/Gemini/Tripo keys | routing of §7 (I3/I2/I6; second Sonnet juror; I10 views flagged; manual-only 3D) |
| No head base, empty hair kit | face layer pack + "2D preview" badge; `hair_custom` via Tripo; export kit without a Head item and with the checklist note |

### 17.5 Windows CI (`windows-latest`, Python 3.14 and 3.13)

`setup.bat` (non-interactive) → `duoskin doctor` → pytest (unit, golden, API, providers, e2e_mock) with outbound sockets blocked → `tools/check_lock.py` → `.bat` files CRLF and ASCII → a portable build smoke test (Phase 2). CHK-X06.

### 17.6 Test day with real keys (pilot, about $50 + about 450 Tripo credits)

Run from Setup → "Test day" (each probe states its cost and asks first); results become capability flags. The list is bible §22 and FAILURE_MODES §6: GPT Image probes (mask + several images, RGBA Image 1, usage, billing of references, Flare→Sunburst vs Flare high); Recraft SVG structure; Tripo balance/upload, one P2 run on a one-sided prop plus the left/right-swapped run, RGBA vs white, `orientation` and `orthographic_projection` A/B, T5 on a raw file token; the Studio tests (numbered shirt/pants on the Block rig and the custom body; `.gltf`+`.bin`, `.fbx`, embedded `.gltf`, `.glb`; the forward-axis arrow; the UGC Validation tool on slabs of 0.05/0.08/0.12 stud); the head base in Studio; judge calibration sets; the router test with real specs; the Claude schema smoke test with the real kit inventory; a clean Windows 11 VM (FM-T11).

### 17.7 v1 acceptance criteria

1. On a clean Windows 11 PC, `start.bat` installs, starts and opens the browser; `doctor` passes or explains each failure.
2. With the three required keys, a B+G duo goes from brief to export kit with every gate, and the kit passes the export gate.
3. The same flow runs in demo mode without keys (export preview only).
4. Every CHK in Appendix A exists, fails closed and has a pass and a fail test.
5. The router test passes on every template.
6. No SOFT check can block, climb the ladder or change an approved part (tests).
7. A kill at any point loses no paid work and never pays twice (recovery tests).

---

## 18. Build order for v1

The order gives a working app early: **settings + plan loop + Gate 1 first**, in mock mode and with real keys. It follows PROPOSAL_DECISION's "Adoption order". Weeks are for one developer with AI help [ESTIMATE]; the decision adds about 8–12 dev days (+1.5–2 weeks) to the 7-week build.

| Milestone | Weeks | Delivers | PROPOSAL_DECISION items landing here | Exit criteria |
|---|---|---|---|---|
| **M0 Skeleton** | 1 | Package layout; `config`, `keystore`, `security`, `winplat`, `logsetup`; DB + migrations; CAS; event bus + SSE; `/api/health`, `/api/state`; `setup.bat`/`start.bat`/`doctor.bat`; `doctor` (CHK-S01…S13); provider registry with all mocks; the web shell (Home, Settings with keys + "Test key", Jobs); demo mode | — | App starts on Windows 3.14 and 3.13; keys stored in Credential Manager; security tests green |
| **M1 Plan loop** | 1–2 | `models/spec.py` + kit enums from builtin/demo kits; prompt registry, compiler, lint, **DNA router + router test**; Claude provider (streamed, structured, fallbacks); L1–L6 handlers; **C1 linter with the hard/soft policy**; engine (scheduler, leases, cache, budget, ladder skeleton); Brief and Plan pages | Adoption 1: outline rule replaced by the garment-cut lint (+ silhouette hooks), hard-vs-soft split, sliding-window registries (tables + checks), planner example removed. Adoption 2–4 (text parts): DNA card split + router test; pair structure + check profiles + anchor rotation + structure log; the wildcard spec | Mock and real plan loops produce 3 lint-clean specs incl. one wildcard; the schema smoke test passes |
| **M2 Gate 1** (first usable build) | 2–3 | Guides (`guide_concept_char`); OpenAI provider; asset loop (draft, Gate A subset, L11 Gate B, rank); I1, C2, C3 (redraw, drift, palette lock → DNA v1, per-duo style sheet, crops); Gate 1 UI with the wildcard label, DNA card, not-buildable panel; Approve / Reimagine / Change (L7 + CHANGE_CONFIRM + CLARIFY) / New plan; BUDGET gate; cost bar; warnings released after the first choice | Adoption 4: the wildcard preview at Gate 1; the DNA lock order (card drafted by the planner, palette overridden from the approved image) | A real B+G brief reaches Gate 1 in ~10 minutes for ~$1–2 [ESTIMATE]; approval locks the card |
| **M3 Part board (2D)** | 3–5 | Prints (I2/R2), SVG path, badges (I6 + slab preview); face parts (R1/I3) + C4 face canvas + 2D previews (no head base) + face registry; hair I4 + T1 + mv.check; accessory I5 + T1 + scale render; the compositor with builtin recipes, fabric library (I7) and procedural folds + template validators; colours tile; Gate 2 UI with the tile drawer, brush mask, alternatives; the dependency table and invalidation; approval hashes | DNA routing in every lane prompt; "Change…" rewrites the card and redoes only the tiles that use the field; checks moved to the earliest stage (hair pairing at plan lint, 2D colours/silhouettes at Gate 2) | Every tile of a real duo reaches READY with finals; a change redoes exactly the parts in the report |
| **M4 Build and 3D** | 5–6 | Mesh worker (import, repair, validate, render views, judge); T3 Tripo with seeds, reconciliation, recovery; **manual mode** (Tripo pack, inbox watcher, import wizard, licence flags); sticker slabs and primitives; hair kit route (L9, fit, polish packs) + Tripo backup; head-base UV warp and pose renders (when the kit exists); final templates; body colours / modesty | — | API and manual meshes pass the same gate; a kill during a P2 poll resumes without a second submit |
| **M5 Duo loop and export** | 6–7 | Renderer (beauty + ID pass, label maps through UVs); duo checks (clone band with degraded mode, interpenetration, part vs concept, IP L13, similarity L14 toggle); L12 judge; Gate 3; export kit, checklist, provenance, secret scan; cross-duo memory | Cross-duo memory after Gate 3 | A real duo exports a kit that the user can take through the Studio tests |
| **M6 Quality loop** | 7–9 | Taste warnings (phone-size top colours, A-vs-B silhouette via the ID pass, optional declared-vs-built ratio, garment layout similarity); calibration data (labels, check stats); the weekly report and Learning page; the regression job and the **variety guard**; version promotion | Adoption 5: ID/label pass, upgrade #3 as warnings only, memory, variety guard | Weekly report shows per-check flag and catch rates; a candidate version can be promoted only through the guard |
| **M7 Calibration screen** | after ≥5 approved duos | Drills as the blind labelling screen (recolours, hair/accessory/print/face swaps, obvious clones and strangers), session limits, threshold tuning to bad-tail bounds (FM-T10) | Adoption 6 | ~200 labels; [DES] thresholds re-tuned |
| Review point | after ~10 duos | The Learning page's revisit triggers for #4 and the structure LRU hint | Adoption 7 | Decision logged |

Parallel from week 1 (outside the app): the head base, the first 10–15 hair styles from a 3D artist with full rights, fabric and fold packs, the taste profile inputs. The pilot/test day (§17.6) runs after M2 (image probes) and after M4 (Tripo and Studio probes).

---

## 19. Open questions

The bible §21 and FAILURE_MODES §6 lists stay open; these are the ones that change code paths in this spec. Each has a safe default already built in.

| # | Question | Default until answered | Settled by |
|---|---|---|---|
| O1 | Does the Studio importer accept an embedded (data-URI) `.gltf` or a `.glb`? Which forward axis do our files need? | `.gltf` + `.bin` + PNG and an embedded-texture `.fbx`; the calibration arrow sets `studio_forward_axis` | FM-T5 |
| O2 | Can DreamSim run as ONNX on onnxruntime without torch? | Degraded clone check, labelled (§10.12) | FM Q1 |
| O3 | Is a mask accepted together with several images on GPT Image 2.5? Is an RGBA Image 1 without a mask read as a mask? | Paste-back / opaque Image 1 / in-canvas layout (bible D17, U26) | FM-T6 |
| O4 | Classic Shirt/Pants on the custom blocky body after a real upload, and the hidden leg rows | Standard Block layout fallback; hidden rows SOFT | FM-T1 |
| O5 | Reuse terms of `BlockyCharacter.fbx` as a head base | `license: unknown` → export banner + manual confirmation | FM Q13 / POL-07 |
| O6 | Does Tripo `balance` exclude `frozen`? Do failed tasks release credits? P2 multi-view on the free web plan? | Reserve `balance − frozen`; manual mode assumes single-image may be needed | FM-T4, Q9, Q12 |
| O7 | Does Sonnet 5 accept `fallbacks: "default"`? | Plain streaming on Sonnet routes (§2 S15) | startup probe |
| O8 | The bible's `PlanSet.brief_constraints` field (§2 S18) | Critic-based must-include coverage | bible v1.2 |
| O9 | Hard ceiling on accessories per character: 3 or 4? | WARN at 3, HARD at ≥4 | the user (FM Q7) |
| O10 | Which thresholds are too strict or too loose on the user's own taste | [DES] defaults from `thresholds.py` | FM-T10 (after ≥5 duos) |
| O11 | Kana/Hangul OCR coverage of the bundled rapidocr models | Glyph detector + `ip_no_text` VLM rule | FM-T9 known negatives |
| O12 | Whether msvc-runtime DLLs are found from inside a venv on a clean PC | `add_dll_directory` + doctor check with a redistributable link | FM-T11 |

**Cross-document edits this spec requires** (to be applied by the owners of those documents): FAILURE_MODES CHK-G1-01 (§2 S1), CHK-G1-04 (S20), CHK-G0-06 and CHK-D03 (S19), and two new plan-lint rows for the rules this spec names `PLN-DNA-01` (≥2 differing CHARACTER DNA fields, §3.2) and `PLN-STR-01` (structure request, §2 S18); bible §15.2 export folder (S5), D15 mesh format (S6), §3.2/§9.3 `brief_constraints` (S18).

---

## Appendix A. Check ownership map

Every `CHK-*` of FAILURE_MODES §7 is implemented by exactly one function, registered in `data/checks.json` with its kind and policy class (§3.1), and has a pass and a fail test.

| Checks | Module / function | Runs in step |
|---|---|---|
| CHK-S01…S13 (startup) | `__main__.py::doctor()` → `winplat`, `imaging/files.py`, `security.py`, `providers/*.probe()`, `mesh/blender.py` | `duoskin doctor`, startup |
| CHK-P01 (prompt lint) | `prompts/compiler.py::lint_prompt` | every `img.draft`, `img.finalize`, `img.repair`, `tripo.edit_view` |
| CHK-P02…P05, P08…P11 (provider pre/post) | `providers/openai_images.py`, `recraft.py`, `tripo.py`, `anthropic_llm.py`, `gemini.py` | every provider call |
| CHK-P06 (budget), P07 (cache key), P12 (3-vote) | `engine/budget.py`, `engine/cache.py`, `engine/ladder.py` | scheduler |
| CHK-G0-01…12 (+ PLN-DNA-01, PLN-STR-01) | `checks/plan_rules.py` via `pipeline/lint.py` | `plan.lint` |
| CHK-G1-01…09 | `imaging/guides.py`, `imaging/checks.py`, `pipeline/concept.py` | I1 loop, `concept.assemble`, `concept.lock` |
| CHK-A01…A15 | `imaging/checks.py`, `imaging/svg.py`, `imaging/ocr.py`, `imaging/similarity.py`, `checks/gate_b.py` | `img.gate_a`, `img.gate_b`, `img.recheck` |
| Gate 2 face / hair / accessory / clothing / colours rows | `imaging/face_canvas.py`, `pipeline/hair.py`, `pipeline/accessory.py`, `roblox/validators.py`, `pipeline/colours.py` | `face.check`, `mv.check`, `clothing.check`, `colours.compose` |
| CHK-B01…B11 | `roblox/validators.py`, `pipeline/build.py` | `clothing.check`, `template.finalize`, `head.check`, `body.compose` |
| CHK-M01…M20 | `mesh/validate.py` (subprocess), `mesh/slab.py`, `mesh/orient.py` | `mesh.import`, `mesh.repair`, `mesh.validate`, `mesh.judge`, `slab.build` |
| CHK-D01…D09 + taste warnings | `pipeline/duo.py`, `checks/taste.py`, `render/sheets.py`, `imaging/similarity.py` | `duo.render`, `duo.checks`, `duo.ip`, `duo.similarity` |
| CHK-E01…E09 | `pipeline/export.py`, `roblox/checklist.py` | `export.validate` |
| CHK-X01…X07 | `engine/recovery.py`, `providers/tripo.py`, `engine/gc.py`, `providers/anthropic_llm.py`, `winplat.py`, CI, `db/db.py` | runtime asserts + tests |

---

## Appendix B. File formats

### B.1 `provenance.json` (export kit)

```json
{
  "schema": "duoskin.provenance/1",
  "app_version": "1.0.0",
  "project": {"id": "prj_…", "name": "…", "combo": "bg", "created_at": "…", "exported_at": "…"},
  "pins": {"models": {"…": "…"}, "prompt_versions": {"I2.print": 3}, "schema_hashes": {"PlanSet": "…"},
           "house_style_version": 1, "style_guide_version": 1, "kit_manifest_sha": "…",
           "thresholds_version": "2026-09-29.2", "rules_version": 1, "roblox_docs_commit": "2026-09-26"},
  "settings": {"reference_similarity_check": false, "use_reference_as_mood": false, "mesh_mode": "api"},
  "dna_card": {"version": 3, "palette_source": "concept_extracted", "…": "…"},
  "spec_sha256": "…",
  "items": [
    {"item_id": "a.acc.0", "type": "Shoulder", "attachment": "RightCollarAttachment", "category": "shoulder",
     "files": [{"path": "A_rin/accessories/shoulder_plush_pet/acc.gltf", "sha256": "…"}],
     "license": "tripo_api_private_commercial",
     "approval_hash": "…",
     "lineage": [
       {"step_kind": "img.draft", "template": "I5.accessory_front@2", "prompt_sha256": "…", "provider": "openai",
        "model": "gpt-image-2.5-flare-2026-09-08", "params": {"size": "1024x1024", "quality": "low", "n": 4,
        "background": "transparent"}, "inputs": ["…"], "outputs": ["…"], "request_id": "req_…",
        "usage": {"…": 0}, "cost_usd": 0.04, "checks": [{"check_id": "CHK-A02", "passed": true}]},
       {"step_kind": "tripo.model", "provider": "tripo", "model": "P2-20260801", "task_id": "…",
        "params": {"face_limit": 3000, "model_seed": 11, "texture_seed": 11, "pbr": false},
        "credits_consumed": 110, "outputs": ["…"]},
       {"step_kind": "mesh.repair", "provider": "code", "handler_version": 4, "facts": {"tris": 2980}}
     ],
     "notes": []}
  ],
  "kit_lineage": [{"kit_asset": "head_base/round/head.fbx", "sha256": "…", "origin": "roblox_reference",
                   "license": "unknown"}],
  "costs": {"total_usd": 11.42, "by_provider": {"anthropic": 4.1, "openai": 3.2, "recraft": 0.9, "tripo": 3.22}},
  "checks_summary": {"hard_failed": 0, "soft_warnings_shown": 2, "overrides": ["CHK-D08:phone_top_colours"]},
  "notes": ["SynthID present in Gemini-derived assets: none", "degraded clone check: false"]
}
```

No key, signed URL, email address or user reference image content ever appears in this file (CHK-E06).

### B.2 `manifest.json` (export kit)

One row per shippable item: `item_id`, `character`, `type` (`Shirt`, `Pants`, `Head`, `Hair`, `Hat`, `Face`, `Neck`, `Shoulder`, `Front`, `Back`, `Waist`, `Body`), `category`, `attachment`, `scale_type: "Classic"`, `target_studs`, `bbox_studs`, `tris`, `texture_px`, `files[] {path, sha256, bytes}`, `upload_channel` (`creator_dashboard` | `studio`), `fee_robux`, `checklist_id`.

### B.3 Tripo pack `asset.json`

```json
{"schema": "duoskin.tripo_pack/1", "asset_id": "a.acc.0", "project_id": "prj_…", "kind": "plush_pet",
 "category": "shoulder", "attachment": "RightCollarAttachment", "target_studs": [1.4, 1.2, 1.1],
 "face_limit": 3000, "views": {"front": "sha256…", "left": "sha256…", "back": "sha256…", "right": "sha256…"},
 "pack_version": 1, "created_at": "…"}
```

---

*End of APP_SPEC v1.0. A change to a module contract, data model or route bumps this version; a change that touches prompts, schemas or thresholds is made in the bible or in FAILURE_MODES first, and this spec follows.*
