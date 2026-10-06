// @ts-check
// The DNA card (APP_SPEC 3.2, 9.2, 12): the world fields both characters share, and each character's own fields side by
// side. A tile can highlight the fields it uses (PROMPT_BIBLE 3.3), so the user sees what a change to a field would redo.
import { h, humanize } from "../dom.js";
import { DNA_LABELS, structureLabel } from "../text.js";

/** The fields in display order. */
const WORLD_FIELDS = ["theme", "pair_structure", "palette_family", "material_family", "detail_level"];
const CHAR_FIELDS = ["shape_language", "colour_plan", "focal_location", "motif_object", "accessory_style", "hair_kit", "energy"];

/** @param {string} key @param {any} v */
function fieldValue(key, v) {
  if (v === undefined || v === null || v === "") return "—";
  if (key === "pair_structure") return structureLabel(String(v));
  if (key === "hair_kit") return humanize(String(v));
  if (["shape_language", "colour_plan", "focal_location", "palette_family", "material_family", "detail_level"].includes(key)) return humanize(String(v));
  return String(v);
}

/**
 * @typedef {object} DnaCardOptions
 * @property {string[]} [highlight] field keys the open tile uses
 * @property {string} [highlightNote] e.g. "used by this tile"
 * @property {boolean} [compact] hide palette and anchors
 * @property {(field: string | null) => void} [onChange] shows the "Change…" buttons
 * @property {string} [title]
 */

/**
 * @param {any} card a DnaCard (GET /api/projects/{id}, GET /api/projects/{id}/specs)
 * @param {DnaCardOptions} [o]
 */
export function dnaCard(card, o = {}) {
  if (!card) return h("p", { class: "muted" }, "The design card appears once a plan exists.");
  const hi = new Set(o.highlight ?? []);
  const world = card.world ?? {};
  /** @param {string} key @param {any} value @param {string} [scope] */
  const row = (key, value, scope = "") => {
    const used = hi.has(key) && (scope !== "world" || WORLD_FIELDS.includes(key));
    return h("div", { class: "dna-row" + (used ? " used" : ""), dataset: { field: key } },
      h("dt", {}, DNA_LABELS[key] ?? humanize(key), used ? h("span", { class: "used-tag" }, o.highlightNote ?? "used here") : null),
      h("dd", {}, fieldValue(key, value),
        o.onChange ? h("button", { type: "button", class: "link-btn", "aria-label": `Change ${DNA_LABELS[key] ?? humanize(key)}`, onclick: () => o.onChange?.(key) }, "Change…") : null));
  };
  const hairKit = card.hair_kit ?? {};
  /** @param {"a"|"b"} k */
  const character = (k) => {
    const c = card[k] ?? {};
    return h("section", { class: `dna-char char-${k}`, "aria-label": `Character ${k.toUpperCase()}` },
      h("h4", {}, h("span", { class: "char-chip" }, k.toUpperCase()), `Character ${k.toUpperCase()}`),
      h("dl", {}, CHAR_FIELDS.map((f) => (f === "hair_kit" ? row(f, hairKit[k]) : c[f] !== undefined ? row(f, c[f]) : null))));
  };
  const swatches = card.palette_hexes && !o.compact
    ? h("ul", { class: "swatches", "aria-label": "Palette" }, Object.entries(card.palette_hexes).map(([id, hex]) => {
      const chip = h("span", { class: "chip", "aria-hidden": "true" });
      chip.style.background = String(hex);
      return h("li", {}, chip, h("span", { class: "swatch-id" }, id), h("span", { class: "swatch-hex" }, String(hex)));
    }))
    : null;
  const anchors = !o.compact && card.anchors?.length
    ? h("div", { class: "dna-anchors" }, h("h4", {}, DNA_LABELS.anchors),
      h("ul", {}, card.anchors.map((/** @type {any} */ a) => h("li", {}, h("strong", {}, humanize(a.kind)), ": ", a.description,
        h("span", { class: "muted" }, ` (A: ${a.on_a}; B: ${a.on_b})`)))))
    : null;
  return h("div", { class: "dna-card" },
    h("div", { class: "dna-head" },
      h("h3", {}, o.title ?? "Design card"),
      h("span", { class: "muted" }, `version ${card.version ?? 1}${card.locked ? ", locked" : ""}`),
      o.onChange ? h("button", { type: "button", class: "btn small", onclick: () => o.onChange?.(null) }, "Change…") : null),
    h("dl", { class: "dna-world" }, WORLD_FIELDS.map((f) => (f === "palette_family" ? row(f, card.palette_family ?? world.palette_family, "world") : world[f] !== undefined ? row(f, world[f], "world") : null))),
    world.story ? h("p", { class: "dna-story" }, h("span", { class: "tag" }, "Story, metadata only (never drawn)"), " ", world.story) : null,
    h("div", { class: "dna-chars" }, character("a"), character("b")),
    anchors, swatches);
}
