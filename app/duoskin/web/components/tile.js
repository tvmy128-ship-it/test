// @ts-check
// A tile: ONE part shown alone, with its state, badges, cost and the three choices every tile has: Approve, Reimagine,
// Change… (APP_SPEC 9.3, 9.5, 10). The tile never decides anything itself; it calls `onAction(action)` and the page sends
// the decision.
//
// What the API gives (models/gate.py GateTile): `assets` is {role: sha}. The roles the part lanes (pipeline/*.py) produce:
//   shirt / pants   flat_front, flat_back, preview_boxes (the Roblox box shape); template and label_map are tool files
//   face            tone_sheet (the face on the head in 4 expressions x 5 skin tones), neutral, blink, mouth_open, happy (own skin
//                   tone); canvas and layer.* are for the drawer. Roles like "neutral_tone_1" also work as a grid.
//   hair            front (on the bald-head guide), hair_only, view.front/left/back/right, views_sheet
//   accessory       front, view.front/left/back/right (or view_*), scale (on-body scale render), badge / slab previews
//   print           final (+ preview100, the 100 px readability preview)
//   colours         swatches, body_front, body_back
// `facts` (pipeline/common.py summarize_checks): checks {hard_failures [{id, evidence}], warnings [], passed, total}, cost_usd,
// kit_match {style_id, iou}, flags. Soft warnings are shown only after the gate's first choice, at most two on the board.
import { h, humanize, money } from "../dom.js";
import { casImage, extForRole } from "../api.js";
import { badge, spinner } from "./ui.js";
import { warningList } from "./warnings.js";
import { partKindLabel, roleLabel, stateLabel, stateTone } from "../text.js";

/** @param {{part_id?: string | null, tile_id?: string, facts?: Record<string, any>}} tile */
export function partKindOf(tile) {
  const pid = tile.part_id || tile.tile_id || "";
  const seg = pid.split(".")[1] || "";
  const map = /** @type {Record<string, string>} */ ({ acc: "accessory", face: "face", hair: "hair", print: "print", shirt: "shirt", pants: "pants", colours: "colours" });
  return map[seg] || String(tile.facts?.kind || "");
}

/** @param {string} pid like "a.acc.0" -> "a" | "b" | "" */
export function characterOf(pid) {
  const c = (pid || "").split(".")[0];
  return c === "a" || c === "b" ? c : "";
}

/** The required-check failures of a tile, as readable lines. @param {{facts?: Record<string, any>}} tile @returns {string[]} */
export function hardFailuresOf(tile) {
  const f = tile.facts || {};
  const list = /** @type {any[]} */ (f.hard_failures || f.checks?.hard_failures || []);
  return list.map((x) => (typeof x === "string" ? x : String(x.evidence || x.message || x.id || x.check_id || "A required check did not pass")));
}

/**
 * The soft warnings a tile may show: only `facts.warnings`, which the API releases (it withholds them before the gate's
 * first choice and then releases at most two for the whole gate, APP_SPEC 9.9). The lane's own full list under
 * `facts.checks.warnings` is never shown. `budget.left` is a second cap for the whole board.
 * @param {{facts?: Record<string, any>}} tile @param {boolean} firstChoice @param {{left: number}} [budget]
 */
export function warningsOf(tile, firstChoice, budget) {
  if (!firstChoice) return [];
  const list = /** @type {any[]} */ ((Array.isArray(tile.facts?.warnings) ? tile.facts?.warnings : []).filter((/** @type {any} */ w) => w && w.visible !== false));
  const take = list.slice(0, budget ? Math.max(0, budget.left) : 2);
  if (budget) budget.left -= take.length;
  return take;
}

/** Roles that are tool files or layers, never shown as pictures. */
const NOT_PICTURES = /^(label_map|body_colors|layer\..*|.*_raw|edit_target|.*\.npz)$/;
/** Roles that only appear in the drawer. */
const DRAWER_ONLY = /^(canvas|template|views_sheet|modesty_layer)$/;
const HERO_FIRST = ["tone_sheet", "front", "view.front", "view_front", "acc_front", "flat_front", "final", "graphic", "swatches", "hero", "main", "preview", "sheet", "concept"];
const STRIP_ORDER = ["hair_only", "neutral", "blink", "mouth_open", "happy", "view.front", "view_front", "view.left", "view_left", "view.back", "view_back", "view.right", "view_right", "scale", "guide_scale", "preview_boxes", "preview100", "readability", "body_front", "body_back", "preview_3d"];

/** @template {{role: string}} T @param {T[]} items @returns {T[]} */
function inOrder(items) {
  const rank = (/** @type {string} */ r) => { const i = STRIP_ORDER.indexOf(r); return i < 0 ? 99 : i; };
  return [...items].sort((a, b) => rank(a.role) - rank(b.role));
}

/** @param {Record<string, string>} assets */
function pickHero(assets) {
  const roles = Object.keys(assets);
  for (const r of HERO_FIRST) if (roles.includes(r)) return r;
  return roles[0];
}

const EXPR = /(neutral|blink|mouth[_-]?open|happy|smile|sleepy|smug|determined|cheerful|soft[_-]?smile)/i;
const TONE = /tone[_-]?(\d+)/i;

/**
 * Split a tile's assets into display sections.
 * @param {string} kind @param {Record<string, string>} assets @param {{full?: boolean}} [o] full: everything that is a picture (the drawer)
 * @returns {{type: "matrix" | "pair" | "hero" | "strip", items: {role: string, sha: string}[], matrix?: {rows: string[], cols: string[], cells: Record<string, string>}}[]}
 */
export function mediaSections(kind, assets, o = {}) {
  const entries = Object.entries(assets).filter(([role]) => !NOT_PICTURES.test(role) && (o.full || !DRAWER_ONLY.test(role))).map(([role, sha]) => ({ role, sha }));
  if (!entries.length) return [];
  const sections = [];
  const heroRole = pickHero(Object.fromEntries(entries.map((e) => [e.role, e.sha])));
  if (kind === "face") {
    const cells = /** @type {Record<string, string>} */ ({});
    const rows = /** @type {string[]} */ ([]);
    const cols = /** @type {string[]} */ ([]);
    const rest = [];
    for (const e of entries) {
      const ex = EXPR.exec(e.role);
      const tn = TONE.exec(e.role);
      if (ex && tn) {
        const row = ex[1].toLowerCase().replace(/[-]/g, "_");
        const col = tn[1];
        if (!rows.includes(row)) rows.push(row);
        if (!cols.includes(col)) cols.push(col);
        cells[`${row}|${col}`] = e.sha;
      } else rest.push(e);
    }
    cols.sort((a, b) => Number(a) - Number(b));
    if (rows.length) sections.push({ type: /** @type {const} */ ("matrix"), items: [], matrix: { rows, cols, cells } });
    const hero = rest.find((e) => e.role === "tone_sheet");
    if (hero) sections.push({ type: /** @type {const} */ ("hero"), items: [hero] });
    const others = rest.filter((e) => e !== hero);
    if (others.length) sections.push({ type: /** @type {const} */ ("strip"), items: inOrder(others) });
    return sections;
  }
  if (kind === "shirt" || kind === "pants") {
    const front = entries.find((e) => e.role === "flat_front") ?? entries.find((e) => /front/.test(e.role));
    const back = entries.find((e) => e.role === "flat_back") ?? entries.find((e) => /back/.test(e.role));
    const pair = [front, back].filter((e) => !!e);
    if (pair.length) sections.push({ type: /** @type {const} */ ("pair"), items: /** @type {any[]} */ (pair) });
    const rest = entries.filter((e) => !pair.includes(e));
    if (rest.length) sections.push({ type: /** @type {const} */ ("strip"), items: inOrder(rest) });
    return sections;
  }
  const hero = entries.find((e) => e.role === heroRole);
  if (hero) sections.push({ type: /** @type {const} */ ("hero"), items: [hero] });
  const rest = entries.filter((e) => e !== hero);
  if (rest.length) sections.push({ type: /** @type {const} */ ("strip"), items: inOrder(rest) });
  return sections;
}

/** @param {string} sha @param {string} role @param {{small?: boolean, label?: string, onOpen?: () => void}} [o] */
export function figure(sha, role, o = {}) {
  const label = o.label ?? roleLabel(role);
  const img = casImage(sha, { alt: label, ext: extForRole(role) });
  const inner = o.onOpen
    ? h("button", { type: "button", class: "img-btn", "aria-label": `Look closer: ${label}`, onclick: o.onOpen }, img)
    : img;
  return h("figure", { class: "fig" + (o.small ? " small" : "") }, inner, h("figcaption", {}, label));
}

/** @param {ReturnType<typeof mediaSections>} sections @param {() => void} [onOpen] @param {string} [kind] */
export function renderSections(sections, onOpen, kind = "") {
  /** @param {string} role */
  const labelOf = (role) => (kind === "hair" && role === "front" ? "On the bald-head guide" : undefined);
  return sections.map((s) => {
    if (s.type === "matrix" && s.matrix) {
      const { rows, cols, cells } = s.matrix;
      return h("div", { class: "matrix", role: "table", "aria-label": "Face in several expressions on different skin tones" },
        h("div", { class: "matrix-row head", role: "row" }, h("span", { role: "columnheader" }), cols.map((c) => h("span", { role: "columnheader" }, `Skin tone ${c}`))),
        rows.map((r) => h("div", { class: "matrix-row", role: "row" }, h("span", { class: "matrix-label", role: "rowheader" }, humanize(r)),
          cols.map((c) => {
            const sha = cells[`${r}|${c}`];
            return h("span", { role: "cell" }, sha ? h("button", { type: "button", class: "img-btn", "aria-label": `Look closer: ${humanize(r)}, skin tone ${c}`, onclick: onOpen }, casImage(sha, { alt: `${humanize(r)} on skin tone ${c}` })) : "");
          }))));
    }
    if (s.type === "pair") return h("div", { class: "pair" }, s.items.map((i) => figure(i.sha, i.role, { onOpen, label: labelOf(i.role) })));
    if (s.type === "hero") return h("div", { class: "hero" }, s.items.map((i) => figure(i.sha, i.role, { onOpen, label: labelOf(i.role) })));
    return h("div", { class: "strip" }, s.items.map((i) => figure(i.sha, i.role, { small: true, onOpen, label: labelOf(i.role) })));
  });
}

/**
 * @typedef {object} TileOptions
 * @property {any} tile a GateTile
 * @property {boolean} firstChoice the gate's first choice was made, so soft warnings may show
 * @property {{left: number}} [warningBudget] shared by all tiles of a board: how many soft warnings may still be shown
 * @property {(action: string) => void | Promise<void>} onAction "approve" | "reimagine" | "change" | "make_manual" | "flip_mirrored" | "select_alternative"
 * @property {() => void} [onOpen] open the drawer
 * @property {boolean} [busy]
 * @property {string} [heading] overrides the label
 */

/** @param {TileOptions} o */
export function tile(o) {
  const t = o.tile;
  const kind = partKindOf(t);
  const facts = t.facts || {};
  const state = String(t.state || "ready");
  const actions = /** @type {string[]} */ (t.allowed_actions || []);
  const hardFails = hardFailuresOf(t);
  const settled = state === "approved";
  const working = state === "generating" || state === "planned";
  const sections = mediaSections(kind, t.assets || {});
  const media = working && !sections.length
    ? h("div", { class: "tile-wait", role: "status" }, spinner(), h("span", {}, "Being made now. This usually takes a minute."))
    : sections.length ? renderSections(sections, o.onOpen, kind) : h("div", { class: "tile-wait" }, h("span", { class: "muted" }, "Nothing to show yet."));
  /** @param {string} action @param {string} label @param {string} [cls] @param {string} [title] @param {boolean} [disabled] */
  const btn = (action, label, cls = "btn", title = "", disabled = false) => {
    const b = h("button", { type: "button", class: cls, title, disabled: disabled || o.busy, "data-action": action }, label);
    b.addEventListener("click", async () => {
      if (b.disabled) return;
      const hadFocus = document.activeElement === b;
      b.disabled = true;                         // a double click must not send two decisions
      b.setAttribute("aria-busy", "true");
      try { await o.onAction(action); } finally {
        if (b.isConnected) {
          b.disabled = disabled || Boolean(o.busy); b.removeAttribute("aria-busy");
          // a disabled button drops the keyboard focus (and a closing dialog cannot give it back to one): put it back
          if (hadFocus && !b.disabled && (!document.activeElement || document.activeElement === document.body)) b.focus();
        }
      }
    });
    return b;
  };
  const approveDisabled = settled || working || state === "failed" || hardFails.length > 0;
  const buttons = [];
  if (actions.includes("approve")) {
    buttons.push(btn("approve", settled ? "Approved ✓" : state === "stale" ? "Approve again" : "Approve", "btn primary",
      hardFails.length ? "A required check failed. Try Reimagine or Change…" : "Approve this part", approveDisabled));
  }
  if (actions.includes("reimagine")) buttons.push(btn("reimagine", "Reimagine", "btn", "Draw this part again with a fresh idea", working));
  if (actions.includes("change")) buttons.push(btn("change", "Change…", "btn", "Describe what should be different", working));
  const extra = [];
  if (actions.includes("make_manual")) extra.push(btn("make_manual", "Make it myself on Tripo", "btn small"));
  if (actions.includes("flip_mirrored")) extra.push(btn("flip_mirrored", "Flip left/right (I checked)", "btn small"));
  if ((actions.includes("select_alternative") || t.alternatives?.length) && o.onOpen) {
    extra.push(h("button", { type: "button", class: "btn small", onclick: o.onOpen }, t.alternatives?.length ? `See ${t.alternatives.length} other version${t.alternatives.length === 1 ? "" : "s"}` : "See other versions"));
  }
  const ch = characterOf(t.part_id || "");
  const costBits = [];
  if (typeof facts.cost_usd === "number") costBits.push(`Spent on this: ${money(facts.cost_usd)}`);
  const km = facts.kit_match;
  if (km && typeof km.iou === "number") costBits.push(`Closest kit hair: ${humanize(String(km.style_id))} (${Math.round(km.iou * 100)}% match)`);
  else if (typeof facts.match_score === "number") costBits.push(`Kit match: ${Math.round(facts.match_score * 100)}%`);
  if (typeof facts.tris === "number") costBits.push(`${facts.tris.toLocaleString()} triangles`);
  const failList = hardFails.length
    ? h("div", { class: "hard-fails", role: "note" }, h("p", { class: "hard-title" }, "Needs fixing first"),
      h("ul", {}, hardFails.map((f) => h("li", {}, f))))
    : null;
  return h("article", { class: `tile state-${state}${ch ? " char-" + ch : ""}`, dataset: { tileId: t.tile_id, state, kind } },
    h("header", { class: "tile-head" },
      h("h3", {}, ch ? h("span", { class: `char-chip ${ch}`, "aria-label": `Character ${ch.toUpperCase()}` }, ch.toUpperCase()) : null, o.heading ?? t.label ?? partKindLabel(kind)),
      badge(stateLabel(state), /** @type {any} */ (stateTone(state)))),
    (t.badges?.length) ? h("div", { class: "tile-badges" }, t.badges.map((b) => badge(String(b), /wild/i.test(b) ? "wild" : /lower|no head|procedural|stale|manual/i.test(b) ? "warn" : "muted"))) : null,
    h("div", { class: "tile-media" }, media),
    failList,
    warningList(warningsOf(t, o.firstChoice, o.warningBudget), o.firstChoice),
    costBits.length ? h("p", { class: "tile-facts muted" }, costBits.join(" · ")) : null,
    state === "waiting_manual" ? h("p", { class: "note warn" }, "Waiting for the 3D file you make on Tripo. The Build page has the pack and the drop box.") : null,
    state === "failed" ? h("p", { class: "note bad" }, "This one did not work out. Try Reimagine, or describe what you want with Change….") : null,
    h("footer", { class: "tile-actions" }, h("div", { class: "row" }, buttons), extra.length ? h("div", { class: "row" }, extra) : null));
}
