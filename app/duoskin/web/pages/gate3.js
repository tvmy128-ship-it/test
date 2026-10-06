// @ts-check
// Gate 3: pick the final duo (#/p/<id>/gate3; APP_SPEC 9.4). Candidates side by side: four sides and a three-quarter view,
// the phone strip, the face in five poses, a 3D viewer with both characters, the judge's notes, the code facts, the IP
// result and the reference-similarity result (or the banner that it is off). Actions: Pick, Change one part, Export.
import { get, tryGet, post, friendly, ApiError, casImage, casUrl, extForRole } from "../api.js";
import { h, humanize, setChildren } from "../dom.js";
import { figure } from "../components/tile.js";
import { pageHeader, badge, note, emptyState, errorNote, notAvailable } from "../components/ui.js";
import { viewerPanel } from "../components/viewer3d.js";
import { decide } from "../components/decisions.js";
import { changeBox } from "../components/changebox.js";
import { followChange } from "../components/changeflow.js";
import { openDialog } from "../components/modal.js";
import { openGatePanels, failedStepsPanel } from "../components/gatepanels.js";
import { warningList } from "../components/warnings.js";
import { toast } from "../components/toast.js";
import { nextAction, roleLabel, checkLabel } from "../text.js";

const SIDES = ["front", "back", "left", "right", "three_quarter"];
const SIDE_LABEL = /** @type {Record<string, string>} */ ({ front: "Front", back: "Back", left: "Left", right: "Right", three_quarter: "Three-quarter" });

/** @param {any} v @returns {string[]} */
function textLines(v) {
  if (!v) return [];
  if (typeof v === "string") return [v];
  if (Array.isArray(v)) return v.map((x) => (typeof x === "string" ? x : x.evidence ? `${humanize(String(x.id || x.check_id || "Check"))}: ${x.evidence}` : String(x.text || x.note || x.message || ""))).filter(Boolean);
  if (typeof v === "object") return Object.entries(v).map(([k, x]) => `${humanize(k)}: ${x && typeof x === "object" ? Object.values(/** @type {any} */ (x)).join(", ") : (x ?? "none")}`);
  return [];
}

/** @param {import("../router.js").PageContext} ctx */
export async function render(ctx) {
  const id = ctx.params.id;
  const body = h("div", { class: "page-body" });
  setChildren(ctx.root, pageHeader({ title: "Pick the final duo", lead: "Look at the finished pair from every side. Pick one, or change a single part and look again.", back: { href: `#/p/${id}`, label: "Your duo" } }), body);
  /** @type {{viewer: {dispose(): void}}[]} */
  let viewers = [];
  ctx.onCleanup(() => viewers.forEach((v) => v.viewer.dispose()));

  const draw = async () => {
    /** @type {any} */ let bundle; let gates = { data: /** @type {any[]} */ ([]), unavailable: false };
    try {
      bundle = await get(`/api/projects/${encodeURIComponent(id)}`, { signal: ctx.signal });
      gates = await tryGet("/api/gates", { query: { project_id: id, state: "open" }, signal: ctx.signal });
    } catch (err) { if (ctx.active()) setChildren(body, errorNote(friendly(err))); return; }
    if (!ctx.active()) return;
    viewers.forEach((v) => v.viewer.dispose());
    viewers = [];
    const project = bundle.project;
    if (gates.unavailable) { setChildren(body, notAvailable("The final pick screen")); return; }
    const gate = gates.data.find((g) => g.kind === "final_pick");
    const panels = openGatePanels(id, gates.data.filter((g) => !["final_pick", "concept", "part_board"].includes(g.kind)), { refresh: draw, navigate: ctx.navigate });
    const failedPanel = await failedStepsPanel(id, draw, ctx.signal);
    if (failedPanel) panels.unshift(failedPanel);
    if (!ctx.active()) return;
    if (!gate) {
      const na = nextAction(project, gates.data);
      setChildren(body, ...panels, emptyState("Nothing to pick right now", project.stage === "duo" ? "The duo is being put together. This page fills in by itself." : "There is no final pick waiting.", h("a", { class: "btn primary", href: na.href }, na.label)));
      return;
    }
    const firstChoice = Boolean(gate.first_choice_at);
    const similarityOn = Boolean(project.settings?.reference_similarity_check);
    const parts = /** @type {any[]} */ ((bundle.parts || []).filter((/** @type {any} */ p) => p.character !== "duo"));
    setChildren(body, ...panels,
      h("div", { class: "candidates" + (gate.tiles.length > 1 ? " multi" : "") }, gate.tiles.map((/** @type {any} */ t, /** @type {number} */ i) => candidate(gate, t, i, project, parts, similarityOn, firstChoice, draw, ctx, viewers))));
  };
  await draw();
  ctx.live(["gate.opened", "gate.updated", "tile.updated", "part.state", "warning.released"], () => { void draw(); }, 500);
}

/**
 * @param {any} gate @param {any} t @param {number} i @param {any} project @param {any[]} parts @param {boolean} similarityOn
 * @param {boolean} firstChoice @param {() => Promise<void>} refresh @param {import("../router.js").PageContext} ctx @param {any[]} viewers
 */
function candidate(gate, t, i, project, parts, similarityOn, firstChoice, refresh, ctx, viewers) {
  // pipeline/duo.py names the pictures "a.front", "phone_strip", "face_poses", "sheet"; "a_front" and "face_pose_3" work too
  /** @type {Record<string, string>} */
  const assets = {};
  for (const [role, sha] of Object.entries(/** @type {Record<string, string>} */ (t.assets || {}))) assets[role.replace(/\./g, "_")] = sha;
  const facts = t.facts || {};
  const picked = t.state === "approved";
  const sideRow = (/** @type {"a"|"b"} */ c) => h("div", { class: `sides char-${c}` }, h("span", { class: `char-chip ${c}` }, c.toUpperCase()),
    SIDES.filter((s) => assets[`${c}_${s}`]).map((s) => h("figure", { class: "fig small" }, casImage(assets[`${c}_${s}`], { alt: `Character ${c.toUpperCase()}, ${SIDE_LABEL[s]}` }), h("figcaption", {}, SIDE_LABEL[s]))));
  const phone = Object.entries(assets).filter(([r]) => /^phone/.test(r));
  const poses = Object.entries(assets).filter(([r]) => /^face_?pose/.test(r)).sort(([a], [b]) => a.localeCompare(b));
  const glbs = Object.entries(assets).filter(([r]) => /glb|mesh|model/.test(r)).sort(([a], [b]) => a.localeCompare(b));
  const viewer = glbs.length ? viewerPanel({ models: glbs.map(([r, sha]) => ({ url: casUrl(sha, extForRole(r)) })), height: 340, caption: "Both characters. Drag to turn, scroll to zoom." }) : null;
  if (viewer) viewers.push(viewer);
  const ip = facts.ip ?? facts.ip_result;
  // pipeline/duo.py stores {ok, fails: [], unsure: [], ocr: []}: an EMPTY list means nothing was found (an empty array is truthy in JS)
  const list = (/** @type {any} */ v) => (Array.isArray(v) ? v : v ? [v] : []);
  const ipUnsure = typeof ip === "object" && ip ? list(ip.unsure) : [];
  const ipFails = typeof ip === "object" && ip ? [...list(ip.fails), ...list(ip.ocr)] : [];
  const ipOk = ip == null ? null : typeof ip === "object" ? ip.passed !== false && ip.ok !== false && !ipUnsure.length && !ipFails.length : Boolean(ip);
  const ipDegraded = typeof ip === "object" && ip && list(ip.ocr).some((/** @type {any} */ x) => /degraded|not installed|unavailable/i.test(String(x)));      // the text reader is missing: a simpler detector guessed
  const ipNote = typeof ip !== "object" || !ip ? "" : ipDegraded && !list(ip.fails).length ? "The text check ran in a simpler mode (the text-reading part is not installed), so it can think it sees writing where there is none. Look at the duo yourself." : ipFails.length ? "Something in the pictures may look like a brand, a known character or writing." : ipUnsure.length ? "The check was not sure about this one." : String(ip.notes || ip.note || "");
  const sim = facts.similarity;
  const simOn = sim && typeof sim === "object" && "on" in sim ? Boolean(sim.on) : similarityOn;
  const simDone = sim && typeof sim === "object" ? Object.keys(sim).some((k) => k !== "on" && sim[k] != null) : Boolean(sim);
  const rebuilt = /** @type {string[]} */ ((facts.rebuilt ?? facts.rebuilt_parts ?? []).map((/** @type {string} */ x) => String(x).replace(/\./g, " ")));
  const judgeNotes = textLines(facts.judge_notes ?? facts.judge?.notes);
  const judgeLevels = Object.entries(/** @type {Record<string, string>} */ (facts.judge?.levels ?? {}));
  // the measured facts, in words: how many required checks passed (and which did not), and whether the two characters look different enough
  const counted = facts.checks && typeof facts.checks === "object" && !Array.isArray(facts.checks) && ("total" in facts.checks || "hard_failures" in facts.checks);
  const checks = counted ? facts.checks : null;                                  // {total, passed, hard_failures}; any other shape is shown as text
  const failedChecks = checks ? list(checks.hard_failures).map((/** @type {any} */ x) => checkLabel(String(x?.id ?? x?.check_id ?? x), "A required check")) : [];
  const cloneText = String(facts.clone_evidence || "");
  const code = [...textLines(facts.code_facts),
    ...(checks ? [Number(checks.total) ? `${checks.passed ?? 0} of ${checks.total} required checks passed` : "", ...failedChecks.map((c) => `Not passed: ${c}`)].filter(Boolean) : textLines(facts.checks)),
    ...(!cloneText ? [] : !/[=;]|\d\.\d|tripped/.test(cloneText) ? [cloneText]      // a sentence already
      : [/tripped:\s*none/i.test(cloneText) ? "The two characters look different enough from each other." : "The two characters may look too alike."]
        .concat(/degraded/i.test(cloneText) ? ["A lighter version of this check was used (the optional picture-comparison model is not installed)."] : []))];
  const blocking = textLines(facts.blocking);
  const pick = h("button", { type: "button", class: "btn primary big", disabled: picked || blocking.length > 0, title: blocking.length ? "Something needs fixing before you can pick this duo." : "", "data-action": "pick", onclick: async () => {
    const r = await decide(gate, t, "pick", { what: `candidate ${i + 1}` });
    if (r.status === "done") toast("Picked. You can export the upload kit now.", { kind: "ok" });
    await refresh();
  } }, picked ? "Picked ✓" : "Pick this duo");
  const change = h("button", { type: "button", class: "btn", "data-action": "change", onclick: () => changePart(gate, t, project.id, parts, refresh) }, "Change one part…");
  const exportBtn = (t.allowed_actions || []).includes("export") ? h("button", { type: "button", class: "btn primary", disabled: !picked, title: picked ? "" : "Pick a duo first", "data-action": "export", onclick: async () => {
    const r = await decide(gate, t, "export");
    if (r.status !== "done") return;
    try { await post(`/api/projects/${project.id}/export`); } catch (e) { if (!(e instanceof ApiError && e.unavailable)) toast(friendly(e), { kind: "bad" }); }
    ctx.navigate(`/p/${project.id}/export`);
  } }, "Export the upload kit") : null;
  return h("article", { class: "candidate" + (picked ? " picked" : ""), dataset: { tileId: t.tile_id } },
    h("header", { class: "candidate-head" }, h("h2", {}, t.label || `Candidate ${i + 1}`), picked ? badge("Your pick", "ok") : null, ...(t.badges || []).filter((/** @type {string} */ b) => !/Reference-similarity/i.test(b) && !/^rebuilt since/i.test(b)).map((/** @type {string} */ b) => badge(b, /DEMO/.test(b) ? "warn" : "muted"))),
    rebuilt.length ? note(`Rebuilt since you last looked: ${rebuilt.map(humanize).join(", ")}. Picking confirms the new version.`, "warn") : null,
    blocking.length ? h("div", { class: "hard-fails", role: "note" }, h("p", { class: "hard-title" }, "Needs fixing first"), h("ul", {}, blocking.map((x) => h("li", {}, x)))) : null,
    assets.sheet ? h("section", {}, h("h3", {}, "The whole duo"), h("div", { class: "hero" }, figure(assets.sheet, "sheet"))) : null,
    h("section", {}, h("h3", {}, "From every side"), sideRow("a"), sideRow("b")),
    phone.length ? h("section", {}, h("h3", {}, "On a phone screen"), h("div", { class: "phone-strip" }, phone.map(([r, sha]) => h("figure", { class: "fig" }, casImage(sha, { alt: roleLabel(r), className: "pixelated" }), h("figcaption", {}, roleLabel(r)))))) : null,
    poses.length ? h("section", {}, h("h3", {}, "The face in five poses"), h("div", { class: poses.some(([r]) => r === "face_poses") ? "hero" : "strip" }, poses.map(([r, sha]) => h("figure", { class: r === "face_poses" ? "fig" : "fig small" }, casImage(sha, { alt: r === "face_poses" ? "The face in five poses" : roleLabel(r) }), r === "face_poses" ? null : h("figcaption", {}, roleLabel(r)))))) : null,
    viewer ? h("section", {}, h("h3", {}, "In 3D"), viewer.el) : null,
    h("section", { class: "reviews" }, h("h3", {}, "What the checks say"),
      judgeNotes.length ? h("div", {}, h("h4", {}, "The judge's notes"), h("ul", {}, judgeNotes.map((l) => h("li", {}, l)))) : h("p", { class: "muted" }, judgeLevels.length ? "The judge looked at the duo and found nothing that would stop an upload." : "The judge has not looked at this duo yet."),
      judgeLevels.length ? h("details", {}, h("summary", {}, "How the judge scored it"), h("dl", { class: "levels" }, judgeLevels.map(([k, v]) => [h("dt", {}, humanize(k)), h("dd", {}, humanize(String(v)))]))) : null,
      code.length ? h("div", {}, h("h4", {}, "Measured facts"), h("ul", {}, code.map((l) => h("li", {}, l)))) : null,
      h("div", {}, h("h4", {}, "Is it too close to something that exists?"), ipOk === null ? h("p", { class: "muted" }, "Not checked yet.") : h("p", {}, badge(ipOk ? "Looks original" : "Needs a look", ipOk ? "ok" : "bad"), " ", ipNote)),
      h("div", {}, h("h4", {}, "Compared with your reference"), simOn ? (simDone ? h("p", {}, typeof sim === "object" ? [badge(sim.passed === false || sim.too_close ? "Close to the reference" : "Not too close", sim.passed === false || sim.too_close ? "warn" : "ok"), " ", String(sim.note || sim.notes || "")] : String(sim)) : h("p", { class: "muted" }, "Not checked yet.")) : note("Reference-similarity check is off", "info"))),
    warningList(facts.warnings, firstChoice),
    h("footer", { class: "tile-actions" }, h("div", { class: "row" }, pick, change, exportBtn)));
}

/** @param {any} gate @param {any} tile @param {string} projectId @param {any[]} parts @param {() => Promise<void>} refresh */
function changePart(gate, tile, projectId, parts, refresh) {
  /** @type {import("../components/modal.js").DialogHandle} */
  let dlg;
  const targets = parts.length ? parts.map((p) => ({ value: p.id, label: p.label })) : undefined;
  const box = changeBox({
    title: "Change one part", targets, defaultTarget: targets?.[0].value, targetLabel: "Which part?", placeholder: "For example: make the hair a little shorter",
    onSubmit: async ({ text, target }) => {
      const label = parts.find((p) => p.id === target)?.label;
      const before = (await get("/api/gates", { query: { project_id: projectId, state: "open" } }).catch(() => [])).map((/** @type {any} */ g) => g.id);
      const r = await decide(gate, tile, "change", { text: label ? `[${label}] ${text}` : text });
      if (r.status === "error") throw new Error(r.message);
      dlg.close();
      if (r.status === "done") await followChange({ projectId, changeId: r.decision?.change_request_id ?? null, afterGateIds: before, onApplied: () => { void refresh(); } });
      await refresh();
    },
    onCancel: () => dlg.close(),
  });
  dlg = openDialog({ title: "Change…", wide: true, body: box });
  box.focusText();
}
