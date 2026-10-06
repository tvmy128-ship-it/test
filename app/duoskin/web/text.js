// @ts-check
// Plain-English wording for everything the server names with codes. One place, so the UI never shows "gate2" or "img.draft".
import { humanize } from "./dom.js";

/** The seven stages of the stage bar: Brief > Plan > Concept > Parts > Build > Duo > Export. */
export const STAGES = [
  { key: "brief", label: "Brief", hint: "Tell us the idea" },
  { key: "plan", label: "Plan", hint: "Three plans are written" },
  { key: "concept", label: "Concept", hint: "You pick a look" },
  { key: "parts", label: "Parts", hint: "Every part, one by one" },
  { key: "build", label: "Build", hint: "Clothes and 3D parts are made" },
  { key: "duo", label: "Duo", hint: "You pick the final pair" },
  { key: "export", label: "Export", hint: "The upload kit" },
];

/** Project.stage -> index of the stage bar step. */
const STAGE_INDEX = { brief: 0, planning: 1, gate1: 2, parts: 3, gate2: 3, building: 4, duo: 5, gate3: 5, exporting: 6, exported: 6 };

/** @param {string} stage */
export function stageIndex(stage) { return /** @type {Record<string, number>} */ (STAGE_INDEX)[stage] ?? 0; }

/**
 * A duo that is picked but not exported yet (the export may be waiting or blocked): the pages must not ask for the pick again.
 * @param {{stage: string}} project @param {any[]} [openGates] the snapshot's gates (`picked`) or a project's gates (with their tiles)
 */
export function duoPicked(project, openGates = []) {
  return project.stage === "gate3" && openGates.some((g) => g.kind === "final_pick" && (g.picked === true || (Array.isArray(g.tiles) && g.tiles.some((/** @type {any} */ t) => t.state === "approved"))));
}

/** What the project is doing, in words. @param {string} stage @param {boolean} [picked] see `duoPicked` */
export function stageSentence(stage, picked = false) {
  if (picked && stage === "gate3") return "You picked the final duo. The export is next";
  return /** @type {Record<string, string>} */ ({
    brief: "Waiting for you to start the plan",
    planning: "Writing three plans",
    gate1: "Waiting for you to pick a concept",
    parts: "Making every part",
    gate2: "Waiting for you to check the parts",
    building: "Building the clothes and 3D parts",
    duo: "Putting the duo together",
    gate3: "Waiting for you to pick the final duo",
    exporting: "Packing the upload kit",
    exported: "Finished: the upload kit is ready",
  })[stage] ?? humanize(stage);
}

/**
 * Where "the next thing to do" for a project lives.
 * @param {{id: string, stage: string}} project
 * @param {any[]} [openGates]
 * @returns {{label: string, href: string, needsYou: boolean}}
 */
export function nextAction(project, openGates = []) {
  const id = project.id;
  const kinds = new Set(openGates.map((g) => g.kind));
  const needsYou = openGates.length > 0;
  if (kinds.has("budget")) return { label: "Decide about the extra cost", href: `#/p/${id}/plan`, needsYou };
  switch (project.stage) {
    case "brief": return { label: "Start the plan", href: `#/p/${id}`, needsYou };
    case "planning": return { label: "Watch the plan", href: `#/p/${id}/plan`, needsYou };
    case "gate1": return { label: "Pick a concept", href: `#/p/${id}/gate1`, needsYou };
    case "parts": return { label: "See the parts", href: `#/p/${id}/board`, needsYou };
    case "gate2": return { label: "Check the parts", href: `#/p/${id}/board`, needsYou };
    case "building": return { label: "Watch the build", href: `#/p/${id}/build`, needsYou };
    case "duo": return { label: "Watch the duo come together", href: `#/p/${id}/build`, needsYou };
    case "gate3": return duoPicked(project, openGates) ? { label: "Open the export page", href: `#/p/${id}/export`, needsYou } : { label: "Pick the final duo", href: `#/p/${id}/gate3`, needsYou };
    case "exporting": return { label: "See the export", href: `#/p/${id}/export`, needsYou };
    default: return { label: "Open the upload kit", href: `#/p/${id}/export`, needsYou };
  }
}

/** Step bar target for each stage-bar position. @param {string} id @param {number} i */
export function stageHref(id, i) {
  return ["", "/plan", "/gate1", "/board", "/build", "/gate3", "/export"].map((s) => `#/p/${id}${s}`)[i];
}

/** @param {string} kind */
export function partKindLabel(kind) {
  return /** @type {Record<string, string>} */ ({
    colours: "Colours and body", face: "Face", hair: "Hair", accessory: "Accessory", print: "Print",
    shirt: "Shirt", pants: "Pants", duo: "The duo",
  })[kind] ?? humanize(kind);
}

/** Tile and part states. @param {string} state */
export function stateLabel(state) {
  return /** @type {Record<string, string>} */ ({
    planned: "Waiting its turn", generating: "Being made", ready: "Ready for you", approved: "Approved",
    stale: "Needs another look", recheck: "Re-checking", needs_human: "Needs your help", waiting_manual: "Waiting for your file",
    building: "Building", built: "Built", failed: "Did not work",
  })[state] ?? humanize(state);
}

/** @param {string} state */
export function stateTone(state) {
  if (["approved", "built"].includes(state)) return "ok";
  if (["failed", "needs_human"].includes(state)) return "bad";
  if (["stale", "recheck", "waiting_manual"].includes(state)) return "warn";
  if (["generating", "building", "planned"].includes(state)) return "busy";
  return "info";
}

/** @param {string} state */
export function jobStateLabel(state) {
  return /** @type {Record<string, string>} */ ({
    running: "Running", waiting_user: "Waiting for you", paused: "Paused", succeeded: "Done", failed: "Failed", cancelled: "Cancelled",
  })[state] ?? humanize(state);
}

/** @param {string} state */
export function stepStateLabel(state) {
  return /** @type {Record<string, string>} */ ({
    pending: "Waiting", ready: "Queued", running: "Working", waiting_remote: "Waiting for the service", waiting_user: "Waiting for you",
    succeeded: "Done", failed: "Failed", cancelled: "Cancelled", superseded: "Replaced",
  })[state] ?? humanize(state);
}

/** @param {string} kind */
export function jobKindLabel(kind) {
  return /** @type {Record<string, string>} */ ({
    setup: "First-time setup", library: "Library", plan: "Plan", parts: "Parts", build: "Build", manual_mesh: "Your own 3D file",
    duo: "Putting the duo together", export: "Export", calibration: "Calibration", regression: "Regression test",
  })[kind] ?? humanize(kind);
}

const STEP_LABELS = /** @type {[RegExp, string][]} */ ([
  [/^plan\.reference/, "Looking at your reference picture"],
  [/^plan\.taste/, "Learning your taste"],
  [/^plan\.planner/, "Writing three plans"],
  [/^plan\.lint/, "Checking the plans against the rules"],
  [/^plan\.critic/, "Judging each plan"],
  [/^plan\.pairwise/, "Comparing the plans"],
  [/^plan\.revise/, "Improving a plan"],
  [/^plan\.select/, "Choosing which plans to show"],
  [/^concept\.char/, "Drawing a character"],
  [/^concept\.assemble/, "Putting the concept sheet together"],
  [/^concept\.gate/, "Getting your concepts ready for you"],
  [/^concept\.lock/, "Locking your concept"],
  [/^part\.start/, "Starting the parts"],
  [/^acc\.board/, "Preparing the accessory tile"],
  [/^hair\.board/, "Preparing the hair tile"],
  [/^face\.finalize/, "Finishing the face"],
  [/^gate2\.open/, "Getting the part board ready for you"],
  [/^change\.interpret|^partchange\.interpret/, "Working out your change"],
  [/^manual\.wait/, "Waiting for your 3D file"],
  [/^mesh\.carry/, "Carrying your edits over to the new model"],
  [/^mesh\.flip/, "Flipping the model left to right"],
  [/^build\.plan/, "Planning the build"],
  [/^build\./, "Building"],
  [/^img\.draft/, "Drawing"],
  [/^img\.gate_a/, "Checking the drawing"],
  [/^img\.gate_b/, "Reviewing the drawing"],
  [/^img\.rank/, "Choosing the best drawing"],
  [/^img\.finalize/, "Polishing the drawing"],
  [/^img\.recheck/, "Re-checking the picture"],
  [/^img\.repair/, "Fixing a detail"],
  [/^face\.assemble/, "Assembling the face"],
  [/^face\.render/, "Showing the face on the head"],
  [/^face\.check/, "Checking the face"],
  [/^tripo\.multiview/, "Making the four views"],
  [/^tripo\.edit_view/, "Fixing one view"],
  [/^tripo\.model/, "Making the 3D model"],
  [/^tripo\.convert/, "Converting the 3D model"],
  [/^mv\./, "Checking the four views"],
  [/^clothing\.compose/, "Making the clothes"],
  [/^clothing\.check/, "Checking the clothes"],
  [/^colours\./, "Making the colour swatches"],
  [/^hair\.kit_match/, "Matching a hair style"],
  [/^hair\.fit/, "Fitting the hair"],
  [/^hair\.register/, "Fitting the hair to the head"],
  [/^polish\.pack/, "Preparing a polish pack"],
  [/^manual\.pack/, "Preparing your Tripo pack"],
  [/^mesh\.import/, "Opening your 3D file"],
  [/^mesh\.repair/, "Tidying the 3D model"],
  [/^mesh\.validate/, "Checking the 3D model"],
  [/^mesh\.judge/, "Comparing the 3D model with the pictures"],
  [/^slab\./, "Making a flat badge"],
  [/^primitive\./, "Making a simple shape"],
  [/^head\./, "Putting the face on the head"],
  [/^template\.finalize/, "Finishing the clothing files"],
  [/^body\./, "Making the body colours"],
  [/^duo\.render/, "Taking the duo photos"],
  [/^duo\.checks/, "Checking the duo"],
  [/^duo\.judge/, "Judging the duo"],
  [/^duo\.ip/, "Checking nothing copies someone else"],
  [/^duo\.similarity/, "Comparing with your reference"],
  [/^duo\.second/, "Asking for a second opinion"],
  [/^duo\.memory/, "Remembering this duo"],
  [/^export\./, "Packing the upload kit"],
  [/^library\.fabric/, "Making a fabric tile"],
  [/^library\.fold/, "Making a fold set"],
  [/^kit\.build_head/, "Building the head base"],
  [/^drill\./, "Preparing a practice round"],
  [/^gate\.wait/, "Waiting for your decision"],
]);

/** @param {string} kind */
export function stepLabel(kind) {
  for (const [re, label] of STEP_LABELS) if (re.test(kind)) return label;
  return humanize(kind.replace(/\./g, " "));
}

/** The plan-loop letters (L1..L6) a step belongs to, for the plan timeline. @param {string} kind */
export function planPhase(kind) {
  /** @type {[RegExp, string][]} */
  const phases = [[/^plan\.reference/, "Read the brief"], [/^plan\.taste/, "Read the brief"], [/^plan\.planner/, "Write plans"],
    [/^plan\.lint/, "Check the rules"], [/^plan\.critic|^plan\.pairwise/, "Judge"], [/^plan\.revise/, "Improve"],
    [/^plan\.select/, "Choose"], [/^img\.|^concept\./, "Draw the concept"]];
  for (const [re, p] of phases) if (re.test(kind)) return p;
  return "Other";
}

/** @param {string} provider */
export function providerLabel(provider) {
  return /** @type {Record<string, string>} */ ({
    anthropic: "Claude (planning and judging)", openai: "OpenAI (pictures)", recraft: "Recraft (faces and prints)",
    tripo: "Tripo (3D models)", gemini: "Gemini (second opinion)", fal: "fal (optional extras)", mock: "Practice mode",
  })[provider] ?? humanize(provider);
}

/** @param {string} provider */
export function providerShort(provider) {
  return /** @type {Record<string, string>} */ ({
    anthropic: "Claude", openai: "OpenAI", recraft: "Recraft", tripo: "Tripo", gemini: "Gemini", fal: "fal", mock: "Practice",
  })[provider] ?? humanize(provider);
}

/** @param {string} kind */
export function gateKindLabel(kind) {
  return /** @type {Record<string, string>} */ ({
    concept: "Pick a concept", part_board: "Check the parts", final_pick: "Pick the final duo", budget: "Extra cost",
    manual_import: "Your 3D file", change_confirm: "Confirm your change", clarify: "A quick question",
    human_review: "Needs a human eye", setup_approval: "Setup picks",
  })[kind] ?? humanize(kind);
}

/** @param {string} combo */
export function comboWords(combo) {
  const w = /** @type {Record<string, string>} */ ({ b: "boy", g: "girl" });
  return `${w[combo[0]] ?? "?"} + ${w[combo[1]] ?? "?"}`;
}

export const PAIR_STRUCTURES = [
  ["auto", "Let the planner choose"],
  ["complement", "Complement: they fit together like two halves"],
  ["leader_chaotic", "Leader and wild one"],
  ["same_club", "Same club: matching team style"],
  ["mirror", "Mirror: opposites of each other"],
  ["seasonal_twins", "Seasonal twins: two seasons or moods"],
  ["object_mascot", "One carries the mascot"],
  ["other", "Something else"],
];

/** @param {string} v */
export function structureLabel(v) {
  const found = PAIR_STRUCTURES.find(([k]) => k === v);
  return found ? found[1].split(":")[0] : humanize(v);
}

/** Labels of the DNA-card fields. */
export const DNA_LABELS = /** @type {Record<string, string>} */ ({
  theme: "Theme", pair_structure: "How the two relate", structure_note: "Note", story: "Story", palette_family: "Colour family",
  material_family: "Fabric family", detail_level: "Detail level", shape_language: "Shape style", colour_plan: "How colour is spread",
  focal_location: "Where the eye lands", motif_object: "Signature object", accessory_style: "Accessory style", energy: "Energy",
  hair_kit: "Hair style", anchors: "What they share",
});

/** Friendly names for the spec fields that show up in a "what changes" list (APP_SPEC 6.2). */
export const SPEC_LABELS = /** @type {Record<string, string>} */ ({
  top: "Shirt", bottom: "Pants", shoes: "Shoes", hair: "Hair", face: "Face", body: "Body", accessories: "Accessories", prints: "Print", makeup: "Makeup",
  base_ref: "main colour", second_ref: "second colour", trim_ref: "trim colour", colour_ref: "colour", shadow_ref: "shadow colour", highlight_ref: "highlight colour",
  sole_ref: "sole colour", accent_ref: "accent colour", legwear_ref: "sock or tights colour", modesty_ref: "under-layer colour", colour_refs: "colours",
  recipe_id: "style", style_id: "style", fabric_id: "fabric", kit_style_id: "hair style", fringe_id: "fringe", back_id: "back of the hair", sleeve: "sleeve length",
  hem: "hem", neckline: "neckline", leg: "leg length", waist: "waist", legwear: "socks and tights", motif: "picture", region: "where it sits", scale: "size",
  size_class: "size", attachment: "where it attaches", category: "type", kind: "kind", description: "description", skin_tone: "skin tone", parting: "parting",
  iris_style: "eye style", lash_style: "lashes", brow_style: "eyebrows", mouth_style: "mouth", nose_style: "nose", cheek_mark: "cheek mark", default_expression: "usual expression",
  eye_shape: "eye shape", highlight_style: "eye sparkle", material: "material", build: "how it is built", front: "front opening", block_layout: "colour blocking", presentation: "boy or girl",
});

/** Which DNA fields each kind of tile uses (PROMPT_BIBLE 3.3), so a tile can highlight them. */
export const DNA_USED_BY = /** @type {Record<string, string[]>} */ ({
  concept: ["shape_language", "motif_object"],
  print: ["shape_language", "detail_level"],
  face: ["shape_language", "detail_level"],
  hair: ["shape_language", "hair_kit"],
  accessory: ["motif_object", "shape_language"],
  shirt: ["material_family"],
  pants: ["material_family"],
  colours: ["palette_family", "colour_plan"],
});

/** Friendly names for part-asset roles shown in tiles and drawers. @param {string} role */
export function roleLabel(role) {
  const r = role.toLowerCase().replace(/\./g, "_");
  const direct = /** @type {Record<string, string>} */ ({
    front: "Front", back: "Back", left: "Left", right: "Right", view_front: "Front", view_back: "Back", view_left: "Left",
    view_right: "Right", flat_front: "Flat front", flat_back: "Flat back", guide_scale: "On the body, to scale", scale: "On the body, to scale",
    preview_3d: "3D preview", preview_boxes: "On the Roblox box shape", sheet: "Concept sheet", a_front: "A front", a_back: "A back",
    b_front: "B front", b_back: "B back", readability: "Small size (100 px)", preview100: "Small size (100 px)", final: "The finished art",
    graphic: "Graphic", badge: "Badge art", slab: "Flat slab preview", primitive: "Simple shape preview", body_front: "Body front",
    body_back: "Body back", swatches: "Colour swatches", palette: "Colour swatches", bald_guide: "On a bald head", front_on_head: "On the head",
    tone_sheet: "On the head: 4 expressions, 5 skin tones", neutral: "Neutral", blink: "Blink", mouth_open: "Mouth open", happy: "Happy",
    canvas: "Face parts, flat", hair_only: "Hair only", views_sheet: "All four views", template: "The Roblox clothing file",
    modesty_layer: "Under-layer", acc_front: "Front", draft_raw: "Draft", final_raw: "Final (untouched)",
  });
  if (direct[r]) return direct[r];
  const tone = r.match(/tone[_-]?(\d)/);
  const expr = r.match(/(neutral|blink|mouth[_-]?open|happy|smile|sleepy|smug)/);
  if (tone || expr) return [expr ? humanize(expr[1]) : "", tone ? `skin tone ${tone[1]}` : ""].filter(Boolean).join(", ");
  return humanize(role.replace(/\./g, " "));
}

/** The words of a warning-severity. @param {string | number | undefined} s */
export function severityLabel(s) {
  const v = typeof s === "number" ? (s >= 3 ? "high" : s === 2 ? "medium" : "low") : String(s || "low");
  return v === "high" ? "Worth a look" : v === "medium" ? "Take a look" : "Small note";
}

/** Licence labels used by the import wizard (APP_SPEC 11.1). @param {string} lic */
export function licenceLabel(lic) {
  return /** @type {Record<string, string>} */ ({
    tripo_api_private_commercial: "Tripo (API): private, commercial use OK",
    tripo_paid_private_commercial: "Tripo paid plan: private, commercial use OK",
    tripo_free_public_ccby_noncommercial: "Tripo FREE plan: public, CC BY 4.0, no commercial use",
    user_made: "Made by you (Blender or elsewhere)",
    unknown: "Licence not known yet",
    "n/a": "Not applicable",
  })[lic] ?? humanize(lic);
}

/** What a check says in plain words. The ids (A_PALETTE, F_LINE_SKIN, CHK-M08) never reach the page: an id this list does not know reads `fallback`. @param {string} id @param {string} [fallback] */
export function checkLabel(id, fallback = "A required check") {
  const words = /** @type {Record<string, string>} */ ({
    A_ALPHA: "Clean see-through background", "CHK-A02": "Clean see-through background", A_COMPONENTS: "One solid piece, not scattered bits", "CHK-A04": "One solid piece, not scattered bits",
    A_MARGIN: "Enough empty space around it", "CHK-A03": "Enough empty space around it", A_OCR: "No writing in the picture", A_GLYPH: "No writing in the picture", "CHK-A06": "No writing in the picture",
    A_PALETTE: "Only the colours of the plan", "CHK-A05": "Only the colours of the plan", A_STROKE: "Thin lines stay visible when small", "CHK-A08": "Thin lines stay visible when small",
    A_SIZE: "The picture has the right size", A_LEAK: "No colours borrowed from the partner", A_SVG: "The vector art is clean", A_SENTINEL: "The background was removed cleanly",
    A_SYMMETRY: "Left and right match", A_BADGE: "The badge is one compact shape", A_HALO: "No glow around the edges", A_PASTE: "The edit left the rest alone", A_DRIFT: "The final matches the chosen draft",
    A_VIEWS: "The four views agree", A_REGISTRY: "Not a copy of something made before", A_REFLEAK: "Does not copy your reference picture", "CHK-A15": "Does not copy your reference picture",
    "CHK-A17": "The 2D face is complete", F_ZONES: "Face features stay in their places", F_LID_COVERS: "A closed eye covers the whole eye", F_LINE_SKIN: "Face lines stand out on every skin tone",
    F_MOUTH_INTERIOR: "The open mouth is painted", F_LASH_LID_SPLIT: "Lashes sit on the lid only", F_LINE_COLOURS: "Each face line has one colour", F_SKIN_TRANSPARENT: "The skin area is see-through",
    F_NO_HAIR: "No hair painted on the face", F_AB_FACE_DIFF: "The two faces look different", F_SHADING: "Shading shows on every skin tone", F_BLUSH: "Blush shows on every skin tone",
    "CHK-B02": "A valid Roblox clothing file", "CHK-B03": "Clothing edges are filled in", "CHK-B04": "Fabric lines meet at the seams", "CHK-B06": "No half-see-through clothing",
    "CHK-B10": "The body colours are complete", "CHK-D02": "The two characters look different enough", "CHK-D04": "Nothing pokes through anything else",
    "CHK-D06": "No logos, brands or writing", "CHK-D09": "Approved parts have not changed since",
    "CHK-M01": "The 3D file opens", "CHK-M02": "One mesh, one material", "CHK-M03": "Small enough for Roblox", "CHK-M04": "The 3D shape is solid and closed",
    "CHK-M05": "No loose pieces", "CHK-M06": "The texture is the right size", "CHK-M07": "No colours painted on the mesh", "CHK-M08": "Faces the right way",
    "CHK-M09": "Fits Roblox's size box", "CHK-M13": "Matches the approved pictures", "CHK-M14": "Fits on the character", "CHK-M16": "The imported file matches the part",
    "CHK-M17": "Hair colours follow the plan", "CHK-M20": "Follows the sticker rules", "CHK-M21": "No leftover guide head in the hair",
    ip_no_brand: "No brand or logo", ip_no_known_character: "Not a known character", ip_no_text: "No writing", ip_age_appropriate: "Suitable for all ages",
    dj_no_leak: "No colours borrowed from the partner", dj_not_clones: "The two do not look like clones", cn_back_view: "The back view has no face",
    cn_front_face: "The front view shows the face", cn_blocky_body: "A Roblox blocky body", cn_views_match: "Front and back match",
  });
  return words[id] ?? fallback;
}
