// @ts-check
// "What would change" lists: spec and DNA-card diffs from GET /api/specs/{id}/diff/{other} and from a ChangeRequest.
import { h, humanize } from "../dom.js";
import { DNA_LABELS } from "../text.js";

const HEX = /^#[0-9a-f]{6}$/i;

/** "/a/dna/shape_language" -> "Character A › Shape style". @param {string} path */
export function labelForPath(path) {
  const segs = path.split("/").filter(Boolean);
  const out = [];
  for (let i = 0; i < segs.length; i += 1) {
    const s = segs[i];
    if (i === 0 && (s === "a" || s === "b")) out.push(`Character ${s.toUpperCase()}`);
    else if (s === "world") out.push("The world");
    else if (s === "dna") continue;
    else if (s === "palette") { out.push("Colour " + (segs[i + 1] ?? "")); i += 1; if (segs[i + 1] === "hex") i += 1; }
    else if (s === "shared_anchors") out.push("What they share");
    else if (/^\d+$/.test(s)) out.push(`#${Number(s) + 1}`);
    else out.push(DNA_LABELS[s] ?? humanize(s));
  }
  return out.join(" › ") || "Everything";
}

/** Readable text for a value; objects are summarised, never shown as JSON. @param {any} v */
export function valueText(v) {
  if (v === null || v === undefined || v === "") return "(empty)";
  if (typeof v === "boolean") return v ? "yes" : "no";
  if (typeof v === "number") return String(v);
  if (typeof v === "string") return HEX.test(v) ? v.toUpperCase() : humanizeEnum(v);
  if (Array.isArray(v)) return v.length ? v.map(valueText).join(", ") : "(none)";
  if (typeof v === "object") {
    const parts = Object.entries(v).slice(0, 4).map(([k, x]) => `${humanize(k)}: ${valueText(x)}`);
    return parts.join("; ") + (Object.keys(v).length > 4 ? "…" : "");
  }
  return String(v);
}

/** Snake-case words become spaced words; real sentences are left alone. @param {string} s */
function humanizeEnum(s) {
  return /^[a-z0-9]+(_[a-z0-9]+)+$/.test(s) ? s.replace(/_/g, " ") : s;
}

/** @param {any} v */
function valueNode(v) {
  const t = valueText(v);
  if (typeof v === "string" && HEX.test(v)) {
    const chip = h("span", { class: "chip", "aria-hidden": "true" });
    chip.style.background = v;
    return h("span", { class: "val" }, chip, t);
  }
  return h("span", { class: "val" }, t);
}

/**
 * @param {{path: string, old: any, new: any}[]} changes
 * @param {{empty?: string, title?: string}} [o]
 */
export function diffList(changes, o = {}) {
  if (!changes?.length) return h("p", { class: "muted" }, o.empty ?? "Nothing changes.");
  return h("ul", { class: "diff-list", "aria-label": o.title ?? "Changes" },
    changes.map((c) => h("li", {}, h("span", { class: "diff-path" }, labelForPath(c.path)),
      h("span", { class: "diff-change" }, valueNode(c.old), h("span", { class: "arrow", "aria-label": "becomes" }, " → "), valueNode(c.new)))));
}

/** The DNA-card diff as a list of changed field paths (DnaCard.diff_from_previous). @param {string[]} paths */
export function pathList(paths) {
  if (!paths?.length) return h("p", { class: "muted" }, "No fields change.");
  return h("ul", { class: "diff-list" }, paths.map((p) => h("li", {}, h("span", { class: "diff-path" }, labelForPath(p)))));
}

const EFFECT = /** @type {Record<string, {label: string, hint: string, tone: string}>} */ ({
  regenerate: { label: "Draw again", hint: "Costs money: a new picture is made", tone: "warn" },
  recompose: { label: "Rebuild", hint: "Free: recoloured or reassembled by the program", tone: "ok" },
  recheck: { label: "Re-check", hint: "Free: only the checks run again", tone: "info" },
});

/**
 * The parts a change would redo (InvalidationReport, APP_SPEC 9.8). Accepts several shapes: a list of
 * {part_id|part, effect}, an object of effect -> [part ids], or an object of part id -> effect.
 * @param {any} report @param {Record<string, string>} [labels] part id -> readable name
 */
export function invalidationList(report, labels = {}) {
  /** @type {{part: string, effect: string}[]} */
  const rows = [];
  const add = (/** @type {string} */ part, /** @type {string} */ effect) => rows.push({ part, effect: String(effect || "recheck").toLowerCase() });
  const src = report?.parts ?? report?.affected ?? report;
  if (Array.isArray(src)) {
    for (const r of src) {
      if (typeof r === "string") add(r, "recheck");
      else add(r.part_id ?? r.part ?? r.id ?? "?", r.effect ?? r.action ?? "recheck");
    }
  } else if (src && typeof src === "object") {
    for (const [k, v] of Object.entries(src)) {
      if (Array.isArray(v) && EFFECT[k.toLowerCase()]) v.forEach((p) => add(typeof p === "string" ? p : p.part_id ?? p.id, k));
      else if (typeof v === "string") add(k, v);
      else if (v && typeof v === "object" && "effect" in v) add(k, /** @type {any} */ (v).effect);
    }
  }
  if (!rows.length) return h("p", { class: "muted" }, "No parts need to be redone.");
  return h("ul", { class: "redo-list" }, rows.map((r) => {
    const e = EFFECT[r.effect] ?? EFFECT.recheck;
    return h("li", {}, h("span", { class: `badge ${e.tone}` }, e.label), " ", h("strong", {}, labels[r.part] ?? humanize(r.part.replace(/\./g, " "))), h("span", { class: "muted" }, ` — ${e.hint}`));
  }));
}
