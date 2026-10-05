// @ts-check
// The tile drawer (APP_SPEC 12): large views, the other versions, the check summary, a provenance summary, per-part
// Reimagine/Change for the face, and the 3D viewer when the part has a model. Opened from "Look closer" on a tile.
import { tryGet, friendly, casUrl, extForRole } from "../api.js";
import { h, money, humanize, setChildren } from "../dom.js";
import { openDialog } from "./modal.js";
import { badge, notAvailable, spinner } from "./ui.js";
import { mediaSections, renderSections, partKindOf, figure, warningsOf, hardFailuresOf } from "./tile.js";
import { runTileAction, FACE_TARGETS } from "./tile-actions.js";
import { viewerPanel } from "./viewer3d.js";
import { warningList } from "./warnings.js";
import { partKindLabel, stateLabel, stateTone } from "../text.js";

/**
 * @param {import("./tile-actions.js").ActionEnv} env
 * @param {boolean} firstChoice
 * @param {(tile: any) => void} [onClosed]
 */
export function openTileDrawer(env, firstChoice, onClosed) {
  const tile = env.tile;
  const kind = partKindOf(tile);
  const actions = /** @type {string[]} */ (tile.allowed_actions || []);
  /** @type {import("./modal.js").DialogHandle} */
  let dlg;
  const act = (/** @type {string} */ a, /** @type {any} */ extra = {}) => runTileAction(env, a, { ...extra, closeDrawer: () => dlg.close() });

  const views = h("section", { class: "drawer-views" }, h("h3", {}, "Large views"), renderSections(mediaSections(kind, tile.assets || {}, { full: true }), undefined, kind));
  const alts = tile.alternatives?.length
    ? h("section", {}, h("h3", {}, "Other versions"),
      h("div", { class: "alts" }, tile.alternatives.map((/** @type {Record<string, string>} */ alt, /** @type {number} */ i) => {
        const roles = Object.keys(alt);
        const hero = roles.find((r) => ["front", "view_front", "flat_front", "graphic", "final"].includes(r)) ?? roles[0];
        return h("div", { class: "alt" }, hero ? figure(alt[hero], hero, { small: true }) : null,
          h("button", { type: "button", class: "btn small", onclick: () => { void act("select_alternative", { choice: String(i) }); } }, `Use version ${i + 2}`));
      })))
    : null;
  const parts = kind === "face" && (actions.includes("reimagine") || actions.includes("change"))
    ? h("section", {}, h("h3", {}, "Change one part of the face"), h("p", { class: "muted" }, "Each part of the face can be redrawn or changed on its own."),
      h("ul", { class: "face-parts" }, FACE_TARGETS.filter((t) => t.value !== "all").map((t) => h("li", {}, h("span", {}, t.label.replace("Just the ", "").replace(/^./, (c) => c.toUpperCase())),
        h("span", { class: "row" },
          actions.includes("reimagine") ? h("button", { type: "button", class: "btn small", onclick: () => { void act("reimagine", { target: t.value }); } }, "Reimagine") : null,
          actions.includes("change") ? h("button", { type: "button", class: "btn small", onclick: () => { void act("change", { target: t.value }); } }, "Change…") : null)))))
    : null;
  const detail = h("section", { class: "drawer-detail" }, h("p", { class: "row" }, spinner(), h("span", {}, "Loading the checks…")));
  const buttons = [
    actions.includes("approve") ? h("button", { type: "button", class: "btn primary", disabled: tile.state === "approved", onclick: () => { void act("approve").then(() => dlg.close()); } }, tile.state === "approved" ? "Approved ✓" : "Approve") : null,
    actions.includes("reimagine") ? h("button", { type: "button", class: "btn", onclick: () => { void act("reimagine"); } }, "Reimagine") : null,
    actions.includes("change") ? h("button", { type: "button", class: "btn", onclick: () => { void act("change"); } }, "Change…") : null,
    actions.includes("make_manual") ? h("button", { type: "button", class: "btn", onclick: () => { void act("make_manual"); } }, "Make it myself on Tripo") : null,
    actions.includes("flip_mirrored") ? h("button", { type: "button", class: "btn", onclick: () => { void act("flip_mirrored"); } }, "Flip left/right (I checked)") : null,
  ];
  dlg = openDialog({
    title: `${tile.label || partKindLabel(kind)}`, wide: true, drawer: true,
    body: [h("p", { class: "row" }, badge(stateLabel(String(tile.state)), /** @type {any} */ (stateTone(String(tile.state)))), ...(tile.badges ?? []).map((/** @type {string} */ b) => badge(b, "muted"))),
      hardFailuresOf(tile).length ? h("div", { class: "hard-fails", role: "note" }, h("p", { class: "hard-title" }, "Needs fixing first"), h("ul", {}, hardFailuresOf(tile).map((f) => h("li", {}, f)))) : null,
      tile.facts?.report ? h("p", { class: "note warn" }, String(tile.facts.report)) : null,
      warningList(warningsOf(tile, firstChoice), firstChoice), views, alts, parts, detail],
    actions: buttons,
  });
  dlg.closed.then(() => onClosed?.(tile));
  void loadDetail(env, tile, detail);
  return dlg;
}

/** @param {import("./tile-actions.js").ActionEnv} env @param {any} tile @param {HTMLElement} host */
async function loadDetail(env, tile, host) {
  const pid = tile.part_id;
  if (!pid) { setChildren(host); return; }
  let res;
  try { res = await tryGet(`/api/projects/${encodeURIComponent(env.projectId)}/parts/${encodeURIComponent(pid)}`); } catch (err) {
    setChildren(host, h("p", { class: "note warn" }, friendly(err)));
    return;
  }
  if (res.unavailable) { setChildren(host, notAvailable("The detailed check list")); return; }
  const { checks = [], provenance = [], links = [] } = res.data || {};
  /** @type {any[]} */
  const hard = checks.filter((/** @type {any} */ c) => c.kind === "hard" || c.kind === "assert");
  const parts = [];
  parts.push(h("h3", {}, "Checks"));
  if (!hard.length) parts.push(h("p", { class: "muted" }, "No required checks have been recorded for this part yet."));
  else {
    const failed = hard.filter((c) => c.status === "failed" || c.status === "not_run").length;
    parts.push(h("p", {}, failed ? badge(`${failed} need fixing`, "bad") : badge(`All ${hard.length} required checks passed`, "ok")));
    parts.push(h("ul", { class: "checks" }, hard.map((c) => h("li", { class: c.status }, badge(c.status === "passed" ? "Passed" : c.status === "not_applicable" ? "Does not apply" : c.status === "not_run" ? "Did not run" : "Needs fixing", c.status === "passed" ? "ok" : c.status === "failed" || c.status === "not_run" ? "bad" : "muted"),
      " ", h("strong", {}, humanize(c.check_id || c.id)), c.evidence ? h("span", { class: "muted" }, ` — ${String(c.evidence).slice(0, 220)}`) : null))));
  }
  if (provenance.length) {
    parts.push(h("h3", {}, "Where each picture came from"));
    parts.push(h("div", { class: "table-wrap" }, h("table", { class: "table compact" },
      h("thead", {}, h("tr", {}, ["Picture", "Made by", "Cost"].map((t) => h("th", {}, t)))),
      h("tbody", {}, provenance.slice(0, 20).map((/** @type {any} */ p) => h("tr", {}, h("td", {}, humanize(p.role)), h("td", {}, [p.source === "mock" ? "Practice mode" : humanize(p.source), p.model ? ` (${p.model})` : ""]), h("td", {}, typeof p.cost_usd === "number" ? money(p.cost_usd) : "—")))))));
  }
  const meshLink = links.find((/** @type {any} */ l) => /mesh|glb|model/i.test(l.role) && ["chosen", "final", "approved", "export", "candidate"].includes(l.status));
  if (meshLink) {
    const { el } = viewerPanel({ models: [{ url: casUrl(meshLink.asset_sha, extForRole("glb")) }], height: 300, caption: "The 3D model for this part" });
    parts.push(h("h3", {}, "3D preview"), el);
  }
  setChildren(host, ...parts);
}
