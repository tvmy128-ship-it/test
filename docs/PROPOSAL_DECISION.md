# Upgrade proposal: decision (29 Sep 2026)

Decision on "DuoSkin workflow upgrades: proposal for the coder" (6 upgrades). Reviewed through four independent lenses (sameness risk, over-strictness, quality value, feasibility and cost), challenged by a devil's advocate, then decided. The app build follows this document.

## Summary

Worth it in part. As written, the proposal is too strict in one place and pulls toward sameness in two others. With the changes below it is neither too strict nor a sameness risk, and it costs about $0.05-0.10 extra per duo.

Where it is too strict or pushes toward sameness:
- #3 as written is the real danger. A fixed 60/30/10 colour split, "one clear focal point" and "3 fashion signals" would give every outfit the same formula (big main colour, small accent, big chest print). They would also fail good bold designs.
- #2 drifts toward sameness if the AI picks the structure freely. It will keep choosing the same one or two.
- #4 drifts toward sameness if one critic keeps its favourite 3 of 12. Its taste becomes the whole catalogue's taste.

What to adopt:
- **#1 DNA card:** adopt it now, split into shared "world" fields and per-character fields. At most 2 fields go into any image prompt.
- **#2 Pair structure:** adopt it, but your brief always wins, the structures rotate, and the structure decides which checks apply.
- **#3 Taste checks:** keep only phone-size colours and the hair/accessory silhouette, and only as warnings or ranking inputs, permanently. Drop the fixed ratio, the focal-point yes/no and the fashion-signal count.
- **#6 Drills:** fold them into the 200-label calibration screen once 5 or more approved duos exist.
- **#4 12 concepts:** skip for now. Instead, make the 3 plans differ and always show one labelled wildcard at Gate 1 ($0). Revisit only if the first ~10 real duos look alike.
- **#5 Showcase:** later, once selling is in scope.

The proposal also exposed bigger problems in the CURRENT workflow. Fixing these matters more than any of the six upgrades:
- The whole-body outline overlap rule (85% or less) is a bug. Shirts and pants are paint on identical boxes, so the rule measures only hair size and pushes every duo toward oversized hair and props.
- "Main colours differ by dE00 15 or more" plus "shared colour anchor within dE00 6" builds every duo as "two contrasting mains + a gold accent". It also blocks matching-colour duos.
- The face and print registries compare against all past work, so they get stricter with every duo.
- The small hair kit (10-15 styles) will cause repeats after about 6 duos.
- The single worked example in the planner prompt (the hyper/deadpan night-market duo) will anchor every plan.

Core rule for avoiding both bad results and sameness:
- Hard checks cover only Roblox rules, buildability, IP/logos and the clone band.
- All taste checks only warn or rank. They only cut the bad tail and never set a target.
- Your pick at the gates, plus a wildcard option, is the main defence against sameness. More novelty rules are not.

Cost is not the limit. The recommended set adds about 8-12 dev days (roughly +1.5-2 weeks on the 7-week build).

## Verdicts

| # | Upgrade | Verdict | Extra cost |
|---|---|---|---|
| 1 | Design DNA lock card | add with changes | About $0 |
| 2 | Pair structure + one-line story | add with changes | About $0 |
| 3 | Taste checks (palette ratio, phone-size colours, hair/prop silhouette, 2 lint rules, AI focal point) | add with changes | About $0 per duo (pixel metrics run locally; the focal-point AI call is dropped; about $0 |
| 4 | 12 concepts -> critic keeps 3 | later | Now: about $0 |
| 5 | Showcase promo image after Gate 3 | later | About $0 |
| 6 | Drills (e.g. recolour an approved duo 20 ways) | add with changes | $0 in API (recolours and swaps are code; DreamSim runs locally) |

### 1. Design DNA lock card (add with changes)

**Value.** The cheapest fix for the 'pasted together' look. Each character is made in 8-12 separate image calls (prints, fabric, face parts, hair view, accessory), and nothing else carries one look across them. The card makes Gate 2 tiles read as one set. It gives the checks a declared target, so they compare what was built with what was declared instead of with a universal taste rule. Logged cards also become a memory of past duos. It is mostly a view of the existing Duo Spec plus a routing table, so add it now as a schema change while the lane prompts are written (weeks 2-5).

**Sameness risk.** As written it is HIGH inside a duo. One card for both characters sends the same shape language, hair style and accessory style into A's and B's calls. That pulls them toward clones, fights the 5-contrast rule and the DreamSim >=0.30 clone band, and causes more expensive plan revisions. The same text in every call also narrows what a 'reimagine' can produce. With the split below it is LOW. Across duos, the planner will reuse its favourite field values, so log the cards. That is handled by the single nearest-duo signal, not by field-matching lints.

**Bad-result risk.** Medium as written, low after the changes:
- Stacking 3-5 DNA fields on top of each call's fixed rules breaks the 5-rules-per-prompt limit and lowers how well each rule is followed.
- Image models cannot follow a written palette ratio.
- A hard 'lock' built from plan text would lock things you never saw and would fight your Gate 2 'Change...' requests.

**Required changes.**

- Split the card into WORLD fields shared by both characters (theme, material/fabric family, detail level, anchor(s); line weight, light and shading bands come from the house-style references) and CHARACTER fields for A and for B (shape language, colour plan, focal location, hair kit id, accessory style, motif object). A and B must differ in at least 2 CHARACTER fields, and these count toward the 5+ contrasts.
- Use fixed values only where code routes or compares them: pair structure, palette family, colour-plan type, focal location, hair kit id, detail level, anchor type. Use short free text for flavour (motif, materials, shape-language wording).
- Routing: each image call gets its fixed rules plus at most 2 DNA fields, with 5 or fewer in total. A code router table plus a unit test fails any prompt template above 5. Palette hexes, palette ratio, colour count, story and pair structure never go into an image prompt; palette snap, compositor areas and OCR enforce them. A character's fields go only to that character's calls. Pass a crop of the approved Gate 1 concept as an image reference instead of more words.
- Order of locking: the planner drafts the card, and after Gate 1 code extracts the palette from the approved front and back concept and overrides the card's colour fields. You approved a picture, not text.
- 'Lock' means a versioned default, not a wall. A Gate 2 or 3 'Change...' rewrites the relevant field for that character, re-lints and redoes only the tiles that use that field. Fix-ladder rung 5 (revise plan) may also rewrite it.
- Log every card with the duo. The planner sees the last 5 cards as 'recently used, prefer something else if the brief allows'. This is a hint, not a lint rule.

**Cost.** About $0.01-0.03 per duo (a few hundred extra Opus planner and critic tokens, plus about 50 tokens per image prompt). 1-2 dev days, plus 0.5 day for the card log.

### 2. Pair structure + one-line story (add with changes)

**Value.** Gives the pair a reason to belong together beyond shared colours, which is what makes a duo read as designed rather than recoloured. It also gives the critic concrete questions, provides a free listing line later, and lets the linked accessory pair express the structure. It is the cheapest variety lever, because it works at the text stage where changes cost cents.

**Sameness risk.** HIGH if the planner picks freely from 6: LLMs tend to settle on 1-2 favourites. The sheet's own example (hyper girl + deadpan boy) is already 'leader + chaotic partner', and it will anchor the planner if it stays in the prompt. Also, under the current colour rules (main colours differ by dE00 >=15 plus a shared colour anchor), 'same club', 'mirror' and 'seasonal twins' can hardly pass. They would be filtered out quietly, and every duo would become 'two contrasting mains + a gold accent'. LOW with the changes below.

**Bad-result risk.** Low to medium:
- 'Mirror' and 'seasonal twins' plans can pass the plan lint, then fail the clone band after the 3D money is spent.
- 'Object + mascot' invites non-human heads or hair the kits cannot build.
- A critic that rewards literal storytelling pushes in extra props, motifs and slogans, against the restraint rule.
- A forced rotation can fight your brief (for example 'twin sisters').

**Required changes.**

- Your brief wins. If it names or implies a structure, all 3 plans use it and vary in other ways. Only when the brief is open do the 3 plans use different structures, as a planner instruction, not quota code. 'Other (planner-defined)' stays allowed.
- Log which structures are picked for the first ~10 duos. Build least-recently-used weighting only if the log shows collapse (for example one structure in more than 35% of duos).
- Each structure selects a check profile (see current_workflow_fixes). 'Same club' or uniform may share a main colour, with contrasts coming from hair, face, cut, print and accessory. 'Mirror' means swapped colour roles or mirrored composition, with a different hair kit style, garment type and accessory category. 'Seasonal twins' means one theme with different season palettes plus the same non-colour contrasts. The linter rejects a plan whose contrasts are mostly colour swaps.
- Limit 'object + mascot' to two human blocky characters, with the mascot as the signature accessory or the linked accessory pair.
- The story is metadata only (plan, critic, later the listing). It never enters an image prompt and never becomes garment text.
- The critic's 2 new questions ('does the front render at phone size show the structure?', 'does the accessory pair express it?') are 1-5 ranking inputs, never pass/fail.
- Remove the single worked example from the planner prompt, or rotate at least 3 examples with different structures, palettes and anchor types.

**Cost.** About $0.01-0.03 per duo (a few hundred planner tokens per plan plus about 600 critic tokens; no images). 0.5-1 dev day, including the check profiles.

### 3. Taste checks (palette ratio, phone-size colours, hair/prop silhouette, 2 lint rules, AI focal point) (add with changes)

**Value.** Two of the checks measure what buyers actually see in a small shop tile. Phone-size top colours answer 'can you tell A from B, and is the planned main colour what shows?'. The A vs B hair and accessory silhouette measures the only shapes that really differ on identical blocky bodies. Both are exact and cost $0 with the ID/label pass, which is needed anyway to replace the broken outline rule. Their logged values also become calibration data. The rest (fixed 60/30/10, 'one clear focal point' AI yes/no, '3 fashion signals') is weak or harmful.

**Sameness risk.** As written this is the BIGGEST sameness driver in the proposal. A fixed 60/30/10 on top of 'main colours differ by dE00 >=15' and 'shared anchor within dE00 6' becomes one formula for every character:
- 60% own main colour, 30% second colour, 10% shared accent.
- Monochrome, 50/50 colour blocking and all-over patterns fail.
- 'One clear focal point' pushes every shirt toward a big chest print.
- A fixed count of 3 fashion signals gives every outfit the same amount of detail.
- Silhouette pressure pushes toward ever bigger hair and props.
- Retrying until checks pass keeps the image model's most typical output.
LOW if the checks only warn and only measure A-vs-B difference or 'built matches declared'.

**Bad-result risk.** Medium to high as blocking checks:
- False fails from skin, hair, shoes and shading bands counted in the ratio.
- Silhouette on flat clothing measures nothing.
- VLM focal-point answers are noisy and biased toward 'yes'.
- Stacked false fails pass only about 57-74% of good duos. They use up the 3-fix cap, trigger Tripo re-generations (about $1.20 each) and plan revisions, and could overwrite parts you already approved at Gate 2.
- The guessed thresholds (15 points, 3 signals) were never tuned.

**Required changes.**

- Make them permanent warnings and ranking inputs, not gates. They never use a fix from the 3-fix cap, never go above rung 1 ($0 auto-fix), never trigger a regenerate or a plan revision, and never auto-change a part approved at Gate 2.
- KEEP phone-size top colours. Area-downscale the front render to about 120-150 px tall, run k-means in CIELAB over clothes and hair with skin excluded and shading bands merged. Warn if the planned main colour is not in the top 2. Fold the existing 'main dE00 >=15' into this measurement, and apply it only under structures that want different mains.
- KEEP the hair and accessory silhouette as an A-vs-B difference only, never as 'bigger or bolder is better'. Precompute a front and side IoU matrix of kit hairstyles once, so a too-similar hair pairing is flagged at plan lint for cents. At Gate 2, use the 2D front-view alpha minus the head guide. After the build, use the ID pass. For accessories, also warn if the accessory is not visible at phone size (at least N px).
- OPTIONAL palette ratio, only as 'built matches declared'. Each character's DNA picks a colour-plan type (60/30/10, 70/20/10, 50/50 block, mono + accent, all-over pattern). Code counts clothes-only pixels on the composited FRONT+BACK template areas, with bracelets and shoes counted as accent. Warn if more than about 15 points off. No universal 60/30/10. Low priority; drop it if it never catches anything.
- DROP the AI focal-point yes/no. Instead the DNA declares each character's focal location (chest, back print, hair, accessory, shoes, face) as a planner hint, preferably different for A and B.
- DROP the fixed '3 fashion signals' lint. If you want it at all, make it a range tied to detail level (minimal 1-2, standard 2-3, maximal 4-5), as a warning only.
- Show at most 2 warnings per gate, after your first choice (as 'approve anyway?'), so they do not bias the decision. Log every override as a label.
- Judge each check by (a) how often it flags duos you approved and (b) how often it catches duos you rejected, never by raw agreement. A check you override more than about 25% of the time is hidden automatically until it is re-tuned.

**Cost.** About $0 per duo (pixel metrics run locally; the focal-point AI call is dropped; about $0.02 if kept as log-only). $0 in retries, because warnings do not escalate. 2-4 dev days; the ID/label pass (about 1-1.5 days) is shared with the outline-rule replacement, so part of it is needed anyway.

### 4. 12 concepts -> critic keeps 3 (later)

**Value.** In theory the strongest variety lever: more ideas before any money is spent. But it is unproven on top of the current 3 plans + critic + reviser + Gate 1 reimagine. Its test is also weak: across 40 different briefs, variety mostly reflects the briefs, and two test arms mean rating about 80 concepts yourself. A free alternative gets most of the value now: when the brief allows, the 3 plans must differ in structure or palette family, and Gate 1 always shows one labelled wildcard.

**Sameness risk.** Can go either way. If one LLM call writes 12 concepts freely, they cluster into 3-4 ideas, and a single critic keeping its top 3 selects its favourite safe look. Over weeks, the critic's taste becomes the whole catalogue's taste (mode collapse). Rough Flare-low thumbnails also favour loud, saturated designs. It only lowers sameness with a code-rolled ingredient grid and selection that keeps the finalists different.

**Bad-result risk.** Medium:
- Rough thumbnails cannot show face or hair quality, so the critic rewards rendering luck.
- Concepts can promise hair the kit cannot build.
- 12 short cards are shallower than 3 full plans.
- It adds 1-2 minutes before Gate 1, and a 12-image sheet adds review work to a gate meant to take about a minute.

**Required changes.**

- Do not build it now. Use the $0 version instead: when the brief allows, the 3 plans differ in structure or palette family, and one of the 3 is a wildcard plan exempt from soft taste preferences. It must still pass buildability, Roblox rules and the clone band. The wildcard gets its own cheap preview and is shown at Gate 1 as a labelled alternative.
- Revisit only if, after about 10 real duos, the log shows sameness (nearest-duo distances shrinking, one structure or palette family dominating) or low Gate 1 first-try approval.
- If revisited:
- Code rolls the ingredient grid, and the 12 are short concept cards (about 200 tokens each), linted before any thumbnail.
- Paint the thumbnails on the same code-drawn guide with kit hair proxies.
- Cull with Sonnet 5 on one 4x3 contact-sheet image.
- The critic sets a quality floor, and code picks the 3 most different survivors (MMR), one of them the wildcard.
- Keep the 9 leftovers so a Gate 1 'New plan' reuses them instead of re-running everything.
- If revisited, test it on the same 40 briefs before and after. Keep it only if your rating or Gate 1 first-try approval improves AND variety on the same briefs does not drop.

**Cost.** Now: about $0.02-0.03 per duo for the wildcard preview (4 Flare-low drafts). If built later: +$0.35-0.6 per plan-loop pass with short cards (+$1-1.2 if all 12 were full plans), 3-4 dev days, plus about 1 day and $50-100 for the A/B run.

### 5. Showcase promo image after Gate 3 (later)

**Value.** Helps selling (Duo Shop page, social posts), but it runs after Gate 3, so it cannot improve the skins, and selling is out of scope for now.

**Sameness risk.** None for the skins. Promo images that look alike are fine as branding. It only becomes a risk if showcase images or their sales data feed the taste profile, critic or registries; then 'what looks good in pose X' starts steering design.

**Bad-result risk.** Low if composited. Medium if an AI edit pass runs over the characters (GPT masks are only suggestions): it could alter the characters or invent props that look included in the purchase, which makes the listing misleading.

**Required changes.**

- Build it only when selling is in scope, after the first perfect duos.
- AI paints only the background (no characters, no text, empty centre), from the DNA world theme. Code composites the real rig renders and draws the label in a fixed font. No AI pass touches character pixels.
- Use a small set of 4-6 pair poses chosen by pair structure. No automatic '50-70% of frame' check is needed, because code places the renders.
- Keep showcase images and any sales data out of the registries, taste profile, critic examples and Gate 3 ranking.

**Cost.** About $0.06-0.10 per duo (4 Flare drafts plus 1 Sunburst high). 2-3 dev days, later.

### 6. Drills (e.g. recolour an approved duo 20 ways) (add with changes)

**Value.** The cheapest source of the labels the plan already needs (about 200) to replace guessed thresholds with your own: the clone band's lower edge, the main-colour and anchor dE00 bands, and the silhouette warnings. It reuses the planned Colourways code. It directly answers the 'too strict' worry, because thresholds then come from where YOU draw the line.

**Sameness risk.** Low to medium. Recolouring the same 1-2 duos tunes thresholds only on colour and only on those duos' shapes. If thresholds are set from favourites ('what I loved') instead of rejects, they turn into targets that pull every duo toward one palette. If drill variants leak into the taste profile or critic examples, new duos drift toward those few designs.

**Bad-result risk.** Low:
- Recolours are the easy cases; the hard 'clone or real duo' line sits in real designs.
- 20 near-identical images cause label fatigue and noisy labels.
- Showing the check's verdict before you label trains you to agree with the machine.
- It needs approved duos first, so it cannot start yet.

**Required changes.**

- Build it as the calibration labelling screen in step 3 (weeks 5-7), not as a side exercise. Start once at least 5 approved duos exist.
- Vary more than colour: recolours plus hair kit swaps, accessory swaps, print swaps and face-variant swaps between A and B. Include a few obvious clones and obvious strangers as anchors.
- Label blind (no check verdicts shown first) as clone / real duo / strangers, plus like or dislike. Log warning overrides from real gates as labels too.
- Use at least 5 base duos covering several combos and colour-plan types. Show at most about 5 variants per base per session, and keep sessions to 10-15 minutes.
- Labels set only the bad-tail bounds (the worst acceptable value), never target values. Drill labels are at most about 25% of the calibration set; the rest comes from real gate decisions.
- Never put drill images into the taste profile, registries or critic examples. Use the 40-brief set only for before/after regression, not for picking thresholds.

**Cost.** $0 in API (recolours and swaps are code; DreamSim runs locally). 1-1.5 dev days on top of the labelling screen the plan already needs, plus 10-15 minutes of your time per session.

## Safeguards (apply to everything)

- Hard vs soft split. HARD (may block or trigger the fix ladder): Roblox rules and validators, buildability from the kits, IP/logo/known-character checks, no stray text, exact reuse of a registered face or print file, and the clone band's lower edge. SOFT (warn or rank only): everything about taste, including all of upgrade 3, main-colour and anchor distances outside the chosen structure's profile, the restraint colour and motif count, and novelty.
- Soft checks never use a fix from the 3-fix cap, never go above rung 1 ($0 auto-fix), never trigger a regenerate or a plan revision, and never auto-change a part you approved at Gate 2.
- Checks only cut the bad tail; they never define a target. Set each threshold at about the 5th percentile of values from duos you approved. No universal ratios, counts or 'one focal point' rules.
- Your brief and your gate edits always win. A brief-named structure overrides rotation, and 'Change...' at any gate rewrites the DNA card (versioned).
- At most 2 warnings per gate, shown after your first choice ('approve anyway?') so they don't bias you. Every override is logged as a label, and a warning you override more than about 25% of the time hides itself until it is re-tuned.
- Judge every check by two numbers, reported weekly: how often it flags duos you approved, and how often it catches duos you rejected. All hard checks together may reject at most about 10% of duos you approved. Any check firing on more than 25-30% of duos is miscalibrated and goes back to warning-only.
- Wildcard at Gate 1. One concept shown is always a labelled wildcard, built without soft taste preferences but passing all hard rules. If you pick it often, give novelty more weight in the critic. This is the main guard against the critic's taste becoming the catalogue's taste.
- Novelty is only a tie-breaker, plus one warning: 'overall 4-side DreamSim is very close to a past duo'. Add no field-combination lints, quotas or kit-use caps; forced novelty pushes the planner into options that don't fit the brief.
- Variety guard in the weekly learning loop. On the SAME 40 briefs before and after, reject a change that improves rating or pass rate but lowers variety by more than about 5%. Measure variety as mean pairwise DreamSim of the 40 results plus counts of distinct structures, hair kit styles and palette families. On the same briefs, this delta measures the system, not the briefs.
- Keep data streams separate. Showcase images and drill variants never enter the registries, taste profile or critic examples. Drill labels are blind and at most about 25% of the calibration set.
- Prompt hygiene. The router unit test fails any image prompt with more than 5 constraints. Palette ratio, hexes, colour count, story and pair structure never go into image prompts. A character's own DNA fields go only to that character's calls.
- Answer repeats by growing the kits (a few hairstyles or pieces a month), not by stricter rules. Give the planner a soft 'recently used' list of hair kit ids, eye and mouth variants and palette families as a hint only.

## Fixes to the current workflow

- Replace the whole-body 'outfit outline overlap <=85%' rule now; it is a bug. Classic Shirt/Pants are paint on identical boxes, so the outline differs only by hair and accessories. Clothing-only overlap is always 100%, and whole-figure overlap is about 0.80-0.95 depending only on hair and accessory volume. The rule fails good duos with similar hair, passes near-clones with different hair, and pushes every duo toward oversized hair and props. Replace it with three checks:
(a) a plan-lint cut rule on the garment recipes: A and B differ in top or bottom garment type, and in at least 2 of sleeve length, crop/hem line, leg length, neckline, open or layered front, and colour-block layout;
(b) a warning-only build check on the compositor label maps, comparing garment-vs-skin coverage and a colour-block layout similarity that ignores the actual colours (for example adjusted Rand index), front and back, tuned on labels;
(c) A-vs-B hair and accessory silhouette overlap from the ID pass (warning).
DreamSim stays the overall clone check.
- Let pair structure pick the check profile. 'Main colours differ by dE00 >=15' plus 'shared colour anchor within dE00 6' currently builds every duo as 'two contrasting mains + gold accent' and blocks matching-colour duos. 'Complement' and 'leader + chaotic' keep main dE00 >=15. 'Same club' or uniform may share a main colour and needs contrasts from hair, face, cut, print and accessory instead. 'Mirror' means swapped colour roles.
- Rotate the anchor type between colour, motif, material/fabric and trim or silhouette detail, instead of always a shared accent colour. The dE00 <=6 anchor check applies only when the anchor is a colour.
- Change the face and print registries to a sliding window: block near-duplicates against the last ~30 duos or the currently listed items, and keep exact-file reuse blocked forever. Compared against all past work, a small kit (2-3 eye shapes, 3-4 mouths) makes the rule stricter with every duo, until around duo 50-100 it rejects good faces or forces odd ones. Track the lint reject rate; a rising rate means grow the kits.
- Remove the single worked example (hyper girl + deadpan boy, night market, lantern gold) from the planner prompt, or rotate at least 3 examples with different structures, palettes and anchor types. One example anchors every plan.
- Add a soft kit-reuse hint. With 10-15 hairstyles and 2 per duo, styles repeat after about 6 duos. Give the planner a 'recently used' list (hair kit ids, eye and mouth variants, fabric packs) and a hair-kit growth plan. Add no hard caps.
- Soften the restraint rule. 'No stray text' stays hard. 'At most 4 main colours' and 'too many motifs' become plan-lint warnings that a plan may override when its DNA declares a 'maximal' detail level or an all-over pattern colour plan.
- Add the cross-duo memory the workflow lacks. After Gate 3, store the 4-side render DreamSim embedding plus the DNA card and kit ids (runs locally, $0). Warn only when the nearest past duo is very close, and use the distance as a tie-breaker in the Gate 3 ranking.
- Add the variety guard to the weekly learning loop (see safeguards). Today any change that makes designs safer and more alike will always 'win' on pass rate.
- Move checks to the earliest cheap stage. Hair pairing is checked at plan lint (precomputed kit-hair IoU matrix), and colours and silhouettes on the 2D parts at Gate 2, before Tripo spend and 30-60 minutes of hair polish.
- Move '<=6 colours' out of the print prompt; palette snap already enforces it. Do the same for anything else code enforces (transparency via API parameter plus the alpha check, framing via the guide), to free prompt slots for the DNA fields.
- Tune thresholds from the ~200 labels plus drills and real gate overrides, not from the 40-brief set. The 40 briefs are for before/after regression only.

## Answers to the coder questions

1. Q1, can the DNA card fit the 5-rules-per-call limit: yes, if the card is split and routed, and code enforces most fields.
- Each call gets its fixed rules plus at most 2 DNA fields, with 5 or fewer in total. A router table in code plus a unit test fails any prompt above 5.
- Never in a prompt: palette hexes, palette ratio, colour count, story, pair structure. Palette snap, compositor areas and OCR handle them.
- House style (line weight, light, shading bands) rides on the style reference images plus a crop of the approved Gate 1 concept.

Mapping (fixed rules + DNA fields = total):
- Concept edit: keep outlines, no text + A and B shape language + motif/theme = 4-5.
- Print or patch: one graphic, transparent, no text + motif object + detail level = 5 (the <=6-colour rule moves to palette snap).
- Fabric: flat, no folds, tileable + material = 4.
- Face part: part id, style ref + shape language + detail level = 4 (eye shape comes from the rig variant).
- Hair front view: keep head guide, match kit-style ref, plain background + that character's shape language = 4 (colour by code recolour).
- Accessory front view: single object on plain background, chunky with no thin parts + motif object + material = 4 (shape language optional as the 5th).
- Sticker: flat sticker, thick outline + motif + shape language = 4.
A character's own fields go only to that character's calls.
2. Q2, clean masks from the rig renders: yes, exact and free, with no AI segmentation, because you own the renderer.
- Add a three.js ID pass: same camera, MeshBasicMaterial, antialias off, NearestFilter, no mipmaps, NoToneMapping, one flat colour per part (body/skin, modesty, head, hair, accessory, sticker). This gives exact hair and accessory masks.
- Clothing: have the compositor write a label map beside each 585x559 template (fabric, secondary block, print, trim, bracelet, shoes, transparent/skin). Render it through the same UVs, stacking shirt over pants over skin, and confirm the order once against a Studio render.
- Palette ratio needs no render. Count clothes-only pixels on the composited FRONT and BACK areas (torso 128x128, each limb 64x128, all at 64 px per stud), skin excluded.
- Phone size: area-downscale the beauty render to about 120-150 px tall and mode-downscale the labels. Run k-means in CIELAB on masks eroded by 1 px, with shading bands merged.
- At Gate 2, the hair mask is the 2D front-view alpha minus the known head-guide mask.
- Don't try to segment Studio screenshots.
3. Q3, planner cost for 12 concepts versus 3 plans (estimates for the same Opus 5 setup; confirm from the API usage fields on pilot day):
- Today's plan loop is about $0.5-1 per pass: planner about $0.4-0.55, critic about $0.25, reviser about $0.24, previews about $0.02.
- 12 short concept cards (only the 3 kept get full plans) add about $0.35-0.6 per pass:
  - grid + cards about $0.25-0.35 on Opus;
  - 12 Flare-low thumbnails $0.07;
  - cull about $0.05-0.09 on Sonnet 5 with one 4x3 contact sheet, or about $0.12-0.31 on Opus with 12 separate images.
- Writing 12 full plans would add about $1-1.2.
- The proposal's '~$0.2 for all six' is too low, mostly because of #4. Caching the kit inventory and system prompt cuts input cost.
Either way it is under about 6% of a $5-12 duo. Dev time and your review time are the real limits, not dollars.
4. Q4, does the outline-cut overlap rule measure anything on flat clothing: no. Classic Shirt/Pants are textures on the same blocky body, so a skirt is just paint on the leg blocks.
- Clothing-only outline overlap is always 100%.
- Whole-figure overlap sits around 0.80-0.95 and depends only on hair and accessory volume.
- So the <=85% rule fails good duos with similar hair, passes near-clones with different hair, and teaches the system to inflate hair and props.
Replace it now with:
(a) a plan-lint garment-recipe cut rule: different top or bottom type, and at least 2 of sleeve length, hem/crop, leg length, neckline, layering, colour-block layout;
(b) a warning-only label-map comparison (garment-vs-skin coverage plus a colour-block layout similarity that ignores colours), front and back;
(c) the A-vs-B hair and accessory silhouette overlap from the ID pass.
DreamSim remains the clone check.
5. Q5, rough build time per upgrade (one coder with AI help, assuming the core lanes exist):
- DNA card 1-2 days, plus 0.5 for the card log.
- Pair structure with check profiles 0.5-1.
- Taste checks as warnings 2-4. The ID/label pass (about 1-1.5) is shared with the outline-rule replacement, so it is needed anyway.
- 12 concepts 3-4, plus about 1 for the A/B harness and $50-100 of A/B runs.
- Showcase 2-3.
- Drills 1-1.5 as the calibration labelling screen.
The recommended set, including the current-workflow fixes, is about 8-12 days, roughly +1.5-2 weeks on the 7-week build.

Cut first: the showcase, then 12 concepts (use the $0 'plans must differ + wildcard' instead). Within the taste checks, cut the fixed 60/30/10, the focal-point yes/no and the fashion-signal count; keep phone-size colours and the hair/accessory silhouette as warnings.

Timing: DNA and pair structure in weeks 2-5, while the lane prompts are written. Taste warnings and drills in the weeks 5-7 quality loop.

## Adoption order

- 1. Now, as current-workflow fixes (cheapest and highest impact):
- Replace the whole-body outline rule with the garment cut lint plus the hair/accessory silhouette.
- Set up the hard-vs-soft check split.
- Change the registries to a sliding window.
- Remove or rotate the single worked example in the planner prompt.
- 2. Weeks 2-5, while the lane prompts are written: upgrade #1 DNA card, split into world and character fields, with the prompt router and its <=5-constraint unit test, locked after Gate 1 from the approved image, and editable by 'Change...'.
- 3. Same window: upgrade #2 pair structure (brief wins; different structures only when the brief is open; 'other' allowed), together with the structure-driven check profiles and anchor-type rotation. Log structure picks.
- 4. Same window, $0: the wildcard plan and its labelled wildcard preview at Gate 1, as the stand-in for upgrade #4.
- 5. Weeks 5-7 quality loop:
- Build the ID/label pass.
- Add upgrade #3 as warnings only: phone-size top colours and the A-vs-B hair/accessory silhouette, plus the optional declared-vs-built ratio.
- Add the cross-duo nearest-duo memory and the learning-loop variety guard.
- 6. Once at least 5 approved duos exist: upgrade #6, drills as the blind calibration labelling screen, feeding the ~200-label threshold tuning.
- 7. After about 10 real duos, review the logs (structure use, nearest-duo distances, Gate 1 first-try approval, per-check false-flag rates). Only then decide on least-recently-used structure weighting and on building upgrade #4 (12 concepts).
- 8. When selling comes into scope: upgrade #5 showcase (background-only AI, code composite).

## Extra cost per duo

Recommended set (#1 + #2 + #3 as warnings + #6 + Gate 1 wildcard): about $0.05-0.10 per duo.
- DNA and structure planner/critic tokens: about $0.02-0.06.
- Wildcard preview (4 Flare-low drafts): about $0.02-0.03.
- Taste checks run locally: $0.
- Drills: $0.
- No retry cost, because warnings do not escalate.
That is about 1% of a $5-12 duo.

If added later:
- #4 (12 concepts): +$0.35-0.6 per plan pass.
- #5 (showcase): +$0.06-0.10.

All six exactly as the proposal wrote them: about $0.5-0.8 per duo, not the claimed $0.2. Add up to about $1.5-2 in the worst case from retries on untuned blocking checks, which is still inside the $15 cap.

The real cost is about 8-12 dev days for the recommended set, plus 10-15 minutes of your time per drill session.

## Devil's-advocate notes

**Devil's advocate: where the four reviewers go wrong, and the right call on each disagreement**

**1. Their fixes for sameness are another stack of strict rules.** Together the reviewers add about a dozen new rules:
- warn when 4 of 6 DNA fields match the last 10 duos
- a new duo must differ from each of the last 10 in 2 or more fields
- a story-embedding novelty check against the last 20
- the least-recently-used structure quota
- a kit cap of about 1 use per 5 duos
- selection that keeps the 3 kept concepts different (MMR)
- a wildcard slot
- a cross-duo diversity alarm

The kit has only 10-15 hairstyles and 2-3 eye shapes, so these rules will push the planner into unused options that don't fit the brief. Forced novelty is how you get bad results. It is the mirror image of the mode collapse they are worried about. **Right call:** novelty only breaks ties in the ranking and only warns. Keep one hard-ish signal, "overall DreamSim is very close to a past duo". Drop the field-combination lints.

**2. They underrate that the biggest sameness drivers are already in the current workflow.** Only reviewer 3 points out that "main dE00 ≥15 + shared colour anchor ≤6" builds every duo as "two contrasting main colours plus a gold accent". It also rules out matching-colour duos, which are popular. Only reviewer 4 points out that the face/print registries get stricter with every duo, because they compare against *all* past work. **Right call:** fix these before adding anything:
- let the pair structure choose which checks apply
- rotate the anchor type (colour, motif, material)
- compare registries against a sliding window of recent duos

These matter more than all six upgrades.

**3. The outline rule is a bug in the current plan, not an upgrade.** The reviewers give different numbers for how much A and B's outlines overlap (78-90%, 95-100%, 0.80-0.93). They all agree the whole-body ≤85% overlap check measures hair volume or nothing. **Right call:** replace it now with two checks: the overlap of hair and accessory shapes, and a check at plan time that the two outfits' garment recipes differ in cut. The disagreement over the number doesn't matter.

**4. Their bars for when a check may block don't make sense.**
- Reviewer 2 wants 40-50 duos in shadow mode.
- Reviewer 4 wants 90% agreement on 100 or more labels.
- Reviewer 1 wants 90% on the 40-brief set.

At a solo pace this is months of waiting. A check that almost never fires also scores 90% "agreement" by default, because most duos pass. **Right call:** accept that the taste checks will be warnings and ranking inputs permanently. Judge each check by (a) how often it flags duos you approved and (b) how often it catches duos you rejected, never by raw agreement. Show at most 1-2 warnings per gate. Also, warnings shown *before* you decide will bias your decision, the same "trains the user to agree with the machine" problem reviewer 3 raised for drills.

**5. Free text or fixed word lists for the DNA card? They contradict each other.** Reviewers 1 and 2 want free text; reviewer 3 wants fixed word lists. Their own novelty checks need fixed values, because free text can't be compared across duos (synonyms). **Right call:** use fixed values only where code routes or compares them (structure, palette family, hair kit id, focal location). Use free text for flavour (motif, materials).

**6. When to lock the DNA card: reviewer 3 is right.** You approve a *picture* at Gate 1, not text. So the planner drafts the card, and after Gate 1 the approved image overrides the palette. Every Gate 2 "Change..." rewrites the card. Only reviewer 3 has this order right.

**7. The 12-concepts test is too optimistic.**
- Across 40 different briefs, mean pairwise DreamSim mostly measures how different the briefs are, not how varied the system is.
- Two arms means rating about 80 duo concepts yourself.
- A 12-image contact sheet adds work at Gate 1, which the plan sheet says takes about a minute per duo.

**Right call:** skip 12 concepts. Use the free option instead: 3 plans that must differ, plus one wildcard. Only revisit 12 concepts if the first ~10 real duos actually look alike.

**8. Pair structure: forced rotation can fight your brief.** If the brief says "twin sisters", a quota that forces 3 different structures wastes 2 of the 3 plans. **Right call:** the brief overrides rotation, rotation applies only when the brief doesn't name a structure, and "other" stays allowed. Log which structures get picked for the first 10 duos before building quota code. The claim that the planner "will mostly pick complement or leader + chaotic partner" is a guess.

**9. Two extra ideas are risky.**
- Rotating style references (reviewer 1) changes the house style that your taste profile set. It needs the full 40-brief test and could lower quality.
- Drills (all four): recolours are the easy cases. The hard "clone or real duo" line sits in real designs. Fold drills into the 200-label calibration screen, after at least 5 approved duos exist.

**10. Cost doesn't decide anything.** The reviewers estimate +$0.35-0.9 per duo against the proposal's $0.2. Either way it is small next to a $5-12 duo. What limits you is dev time (11-17 days, about +2-3 weeks on the 7-week build) and your own time.

**Net answer to your question:** As written, the proposal is too strict in #3: the fixed 60/30/10 split, one required focal point and 3 fashion signals would make every outfit follow the same formula and cause false failures. #2 and #4 would drift toward sameness without rotation or diverse selection. With the changes it is **not too strict**. Worth doing:
- #1, split into shared world fields and per-character fields, with at most 2 fields in any prompt
- #2 with rotation, where your brief wins
- #3 as logging and warnings only: phone-size colours and hair/prop silhouette, no ratio targets

Plus the three fixes to the current workflow (points 2 and 3). Skip #4 and #5 for now, and fold #6 into calibration. Do not add the pile of novelty lints; your pick at the gates is the best defence against sameness.
