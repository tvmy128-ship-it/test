// @ts-check
// A tile: ONE part shown alone, with its state, badges, cost and the three choices every tile has: Approve, Reimagine,
// Change… (APP_SPEC 9.3, 9.5, 10). The tile never decides anything itself; it calls `onAction(action)` and the page sends
// the decision.
//
// What the API gives (models/gate.py GateTile): `assets` is {role: sha}. Roles the layouts recognise (others still show):
//   shirt / pants   flat_front, flat_back (+ preview_3d)
//   face            expression x skin-tone grid: roles such as "neutral_tone_1", "blink_tone_3", "mouth_open_tone_5", "happy_tone_2"
//   accessory       view_front, view_left, view_back, view_right, scale (on-body scale render), badge / slab / primitive previews
//   hair            front (on the bald-head guide) and view_front/left/back/right
//   print           graphic (+ readability, the 100 px preview)
//   colours         swatches, body_front, body_back
// `facts` may carry: cost_usd, match_score, tris, checks {passed, failed}, hard_failures [], warnings [] (released only
// after the gate's first choice), estimate_usd (what Reimagine would cost).
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

const HERO_FIRST = ["front", "view_front", "flat_front", "graphic", "final", "hero", "main", "preview", "sheet", "concept", "swatches"];

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
 * @param {string} kind @param {Record<string, string>} assets
 * @returns {{type: "matrix" | "pair" | "hero" | "strip", items: {role: string, sha: string}[], matrix?: {rows: string[], cols: string[], cells: Record<string, string>}}[]}
 */
export function mediaSections(kind, assets) {
  const entries = Object.entries(assets).map(([role, sha]) => ({ role, sha }));
  if (!entries.length) return [];
  const sections = [];
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
    if (rest.length) sections.push({ type: /** @type {const} */ ("strip"), items: rest });
    return sections;
  }
  if (kind === "shirt" || kind === "pants") {
    const front = entries.find((e) => /flat_front|front/.test(e.role));
    const back = entries.find((e) => /flat_back|back/.test(e.role));
    const pair = [front, back].filter((e) => !!e);
    if (pair.length) sections.push({ type: /** @type {const} */ ("pair"), items: /** @type {any[]} */ (pair) });
    const rest = entries.filter((e) => !pair.includes(e));
    if (rest.length) sections.push({ type: /** @type {const} */ ("strip"), items: rest });
    return sections;
  }
  const heroRole = pickHero(assets);
  const hero = entries.find((e) => e.role === heroRole);
  if (hero) sections.push({ type: /** @type {const} */ ("hero"), items: [hero] });
  const rest = entries.filter((e) => e !== hero);
  if (rest.length) sections.push({ type: /** @type {const} */ ("strip"), items: rest });
  return sections;
}

/** @param {string} sha @param {string} role @param {{small?: boolean, onOpen?: () => void}} [o] */
export function figure(sha, role, o = {}) {
  const img = casImage(sha, { alt: roleLabel(role), ext: extForRole(role) });
  const inner = o.onOpen
    ? h("button", { type: "button", class: "img-btn", "aria-label": `Look closer: ${roleLabel(role)}`, onclick: o.onOpen }, img)
    : img;
  return h("figure", { class: "fig" + (o.small ? " small" : "") }, inner, h("figcaption", {}, roleLabel(role)));
}

/** @param {ReturnType<typeof mediaSections>} sections @param {() => void} [onOpen] */
export function renderSections(sections, onOpen) {
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
    if (s.type === "pair") return h("div", { class: "pair" }, s.items.map((i) => figure(i.sha, i.role, { onOpen })));
    if (s.type === "hero") return h("div", { class: "hero" }, s.items.map((i) => figure(i.sha, i.role, { onOpen })));
    return h("div", { class: "strip" }, s.items.map((i) => figure(i.sha, i.role, { small: true, onOpen })));
  });
}

/**
 * @typedef {object} TileOptions
 * @property {any} tile a GateTile
 * @property {boolean} firstChoice the gate's first choice was made, so soft warnings may show
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
  const hardFails = /** @type {any[]} */ (facts.hard_failures || []);
  const settled = state === "approved";
  const working = state === "generating" || state === "planned";
  const sections = mediaSections(kind, t.assets || {});
  const media = working && !sections.length
    ? h("div", { class: "tile-wait", role: "status" }, spinner(), h("span", {}, "Being made now. This usually takes a minute."))
    : sections.length ? renderSections(sections, o.onOpen) : h("div", { class: "tile-wait" }, h("span", { class: "muted" }, "Nothing to show yet."));
  /** @param {string} action @param {string} label @param {string} [cls] @param {string} [title] @param {boolean} [disabled] */
  const btn = (action, label, cls = "btn", title = "", disabled = false) =>
    h("button", { type: "button", class: cls, title, disabled: disabled || o.busy, "data-action": action, onclick: () => o.onAction(action) }, label);
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
  if (typeof facts.match_score === "number") costBits.push(`Kit match: ${Math.round(facts.match_score * 100)}%`);
  if (typeof facts.tris === "number") costBits.push(`${facts.tris.toLocaleString()} triangles`);
  const failList = hardFails.length
    ? h("div", { class: "hard-fails", role: "alert" }, h("p", { class: "hard-title" }, "Needs fixing first"),
      h("ul", {}, hardFails.map((f) => h("li", {}, typeof f === "string" ? f : String(f.evidence || f.message || f.check_id || "A required check did not pass")))))
    : null;
  return h("article", { class: `tile state-${state}${ch ? " char-" + ch : ""}`, dataset: { tileId: t.tile_id, state, kind } },
    h("header", { class: "tile-head" },
      h("h3", {}, ch ? h("span", { class: `char-chip ${ch}`, "aria-label": `Character ${ch.toUpperCase()}` }, ch.toUpperCase()) : null, o.heading ?? t.label ?? partKindLabel(kind)),
      badge(stateLabel(state), /** @type {any} */ (stateTone(state)))),
    (t.badges?.length) ? h("div", { class: "tile-badges" }, t.badges.map((b) => badge(String(b), /wild/i.test(b) ? "wild" : /lower|no head|procedural|stale|manual/i.test(b) ? "warn" : "muted"))) : null,
    h("div", { class: "tile-media" }, media),
    failList,
    warningList(facts.warnings, o.firstChoice),
    costBits.length ? h("p", { class: "tile-facts muted" }, costBits.join(" · ")) : null,
    state === "waiting_manual" ? h("p", { class: "note warn" }, "Waiting for the 3D file you make on Tripo. The Build page has the pack and the drop box.") : null,
    state === "failed" ? h("p", { class: "note bad" }, "This one did not work out. Try Reimagine, or describe what you want with Change….") : null,
    h("footer", { class: "tile-actions" }, h("div", { class: "row" }, buttons), extra.length ? h("div", { class: "row" }, extra) : null));
}
