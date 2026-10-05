// @ts-check
// Gate 1: pick a concept (#/p/<id>/gate1; APP_SPEC 9.2). Three plan cards, each with the 4-up sheet (A front, A back,
// B front, B back), the design card summary, what the kits cannot build as drawn, must-include coverage and the choices
// Approve / Reimagine (A only, B only or both) / Change… plus the gate's "New plan".
import { get, tryGet, friendly, casImage, extForRole } from "../api.js";
import { h, money, humanize, uid, setChildren } from "../dom.js";
import { pageHeader, badge, emptyState, errorNote, notAvailable } from "../components/ui.js";
import { openDialog } from "../components/modal.js";
import { dnaCard } from "../components/dnacard.js";
import { decide } from "../components/decisions.js";
import { changeBox } from "../components/changebox.js";
import { followChange } from "../components/changeflow.js";
import { warningList } from "../components/warnings.js";
import { toast } from "../components/toast.js";
import { openGatePanels } from "../components/gatepanels.js";
import { structureLabel, roleLabel, DNA_USED_BY, nextAction } from "../text.js";
import { state } from "../store.js";

const SHEET_ROLES = ["a_front", "a_back", "b_front", "b_back"];
const SCOPES = [{ value: "both", label: "Both characters" }, { value: "a", label: "Character A only" }, { value: "b", label: "Character B only" }];

/** @param {any} v @returns {string[]} */
function lines(v) {
  if (!v) return [];
  if (typeof v === "string") return [v];
  if (Array.isArray(v)) return v.map((x) => (typeof x === "string" ? x : String(x.element || x.label || x.name || x.text || x.description || "")) + (typeof x === "object" && x.reason ? ` — ${x.reason}` : "")).filter(Boolean);
  return [];
}

/** @param {any} mi @returns {{text: string, covered: boolean}[]} */
function coverage(mi) {
  if (!mi) return [];
  if (Array.isArray(mi)) return mi.map((x) => (typeof x === "string" ? { text: x, covered: true } : { text: String(x.text || x.line || ""), covered: x.covered !== false && x.missing !== true }));
  return [...(mi.covered || []).map((/** @type {string} */ t) => ({ text: t, covered: true })), ...(mi.missing || []).map((/** @type {string} */ t) => ({ text: t, covered: false }))];
}

/** @param {import("../router.js").PageContext} ctx */
export async function render(ctx) {
  const id = ctx.params.id;
  const body = h("div", {});
  setChildren(ctx.root, pageHeader({ title: "Pick a concept", lead: "Three plans were drawn. Approve the one you like, try again, or say what to change. Nothing big is built until you approve.", back: { href: `#/p/${id}`, label: "Your duo" } }), body);

  const draw = async () => {
    /** @type {any} */ let bundle; let gates = { data: /** @type {any[]} */ ([]), unavailable: false }; let specs = { data: /** @type {any[]} */ ([]), unavailable: false };
    try {
      bundle = await get(`/api/projects/${encodeURIComponent(id)}`, { signal: ctx.signal });
      gates = await tryGet("/api/gates", { query: { project_id: id, state: "open" }, signal: ctx.signal });
      specs = await tryGet(`/api/projects/${encodeURIComponent(id)}/specs`, { signal: ctx.signal });
    } catch (err) { if (ctx.active()) setChildren(body, errorNote(friendly(err))); return; }
    if (!ctx.active()) return;
    const project = bundle.project;
    if (gates.unavailable) { setChildren(body, notAvailable("The concept screen")); return; }
    const concept = gates.data.find((g) => g.kind === "concept");
    const other = openGatePanels(id, gates.data.filter((g) => g.kind !== "concept"), { refresh: draw, navigate: ctx.navigate });
    if (!concept) {
      const na = nextAction(project, gates.data);
      setChildren(body, ...other, emptyState("Nothing to pick right now", project.stage === "planning" ? "The plans are still being made. This page fills in by itself." : "There is no concept waiting for you. Here is where this duo is.",
        h("a", { class: "btn primary", href: na.href }, na.label)));
      return;
    }
    const byId = new Map(/** @type {any[]} */ (specs.data).map((r) => [r.spec.id, r]));
    const firstChoice = Boolean(concept.first_choice_at);
    const dnas = concept.tiles.map((/** @type {any} */ t) => byId.get(t.facts?.spec_id)?.dna_card ?? null);
    const read = [...new Set(dnas.map((/** @type {any} */ d) => d?.world?.pair_structure).filter(Boolean))].map((s) => structureLabel(String(s)));
    const readFromFacts = concept.tiles.flatMap((/** @type {any} */ t) => lines(t.facts?.brief_read_as)).map((x) => structureLabel(x));
    const readAs = read.length ? read : [...new Set(readFromFacts)];
    const spent = Math.max(concept.spent_usd_at_open || 0, 0);
    const newPlan = h("button", { type: "button", class: "btn", onclick: () => newPlanDialog(concept, refreshAll) }, "None of these: make a new plan");
    const refreshAll = async () => { await draw(); };
    setChildren(body, ...other,
      h("section", { class: "gate-summary" },
        h("p", {}, h("strong", {}, "Your brief was read as: "), readAs.length ? readAs.join(", ") : "the planner's choice"),
        h("p", { class: "muted" }, `Spent so far on this duo: ${money(state.project?.project?.spent_usd ?? project.spent_usd ?? spent)}. Approving one plan starts the final pictures; the cost bar updates as they are made.`)),
      h("div", { class: "plan-cards" }, concept.tiles.map((/** @type {any} */ t, /** @type {number} */ i) => planTile(concept, t, i, dnas[i], firstChoice, project, refreshAll, ctx))),
      h("div", { class: "row center" }, newPlan));
  };
  await draw();
  ctx.live(["gate.opened", "gate.updated", "tile.updated", "warning.released", "spec.updated"], () => { void draw(); });
}

/** @param {any} concept @param {() => Promise<void>} refresh */
function newPlanDialog(concept, refresh) {
  const why = h("textarea", { id: "np-why", rows: "3", maxlength: "1000", placeholder: "Optional: what was wrong with these? For example: too busy, I wanted something calmer." });
  /** @type {import("../components/modal.js").DialogHandle} */
  let dlg;
  const go = h("button", { type: "button", class: "btn primary", onclick: async () => {
    go.disabled = true;
    const r = await decide(concept, concept.tiles[0], "new_plan", { text: why.value.trim() });
    if (r.status === "done") { dlg.close(); toast("Making three new plans. They will appear here.", { kind: "ok" }); }
    await refresh();
    go.disabled = false;
  } }, "Make three new plans");
  dlg = openDialog({ title: "Make a new plan?", body: [h("p", {}, "The three plans you see now are set aside and the planner tries again, avoiding what you did not like. Planning steps cost a little."), h("label", { for: "np-why" }, "What was wrong with these? (optional)"), why], actions: [h("button", { type: "button", class: "btn", onclick: () => dlg.close() }, "Not now"), go] });
  why.focus();
}

/**
 * @param {any} gate @param {any} tile @param {number} i @param {any} dna @param {boolean} firstChoice @param {any} project
 * @param {() => Promise<void>} refresh @param {import("../router.js").PageContext} ctx
 */
function planTile(gate, tile, i, dna, firstChoice, project, refresh, ctx) {
  const facts = tile.facts || {};
  const assets = /** @type {Record<string, string>} */ (tile.assets || {});
  const notBuildable = lines(facts.not_buildable);
  const cover = coverage(facts.must_include);
  const wildcard = facts.wildcard === true || (tile.badges || []).some((/** @type {string} */ b) => /wild/i.test(b));
  // a character whose drawings all failed the required checks (the plan loop says which, in words)
  const failedBy = Object.entries(facts.failed || {}).map(([c, f]) => `Character ${c.toUpperCase()}: ${f?.message || "no usable drawing yet"}`);
  const hardLines = failedBy.length ? failedBy : lines(facts.hard_failures).map((x) => humanize(x));
  const noteLines = [...lines(facts.notice), ...lines(facts.notes)].filter(Boolean);
  const ackId = uid("ack");
  const ack = h("input", { type: "checkbox", id: ackId });
  const ackNote = h("p", { class: "note bad", role: "alert", hidden: true }, "Please tick the box to confirm you understand.");
  const sheet = SHEET_ROLES.filter((r) => assets[r]).length
    ? h("div", { class: "sheet-4up" }, SHEET_ROLES.filter((r) => assets[r]).map((r) => h("figure", { class: "fig" }, casImage(assets[r], { alt: `Plan ${i + 1}: ${roleLabel(r)}`, ext: extForRole(r) }), h("figcaption", {}, h("span", { class: `char-chip ${r[0]}` }, r[0].toUpperCase()), roleLabel(r).replace(/^[AB] /, "")))))
    : Object.keys(assets).length ? h("div", { class: "hero" }, h("figure", { class: "fig" }, casImage(Object.values(assets)[0], { alt: `Plan ${i + 1}` }))) : h("div", { class: "tile-wait muted" }, hardLines.length ? "No drawing of this plan could be used, so there is nothing to look at." : "The drawings are still being made.");
  const alts = /** @type {Record<string, string>[]} */ (tile.alternatives || []);
  const altBlock = alts.length ? h("details", { class: "alts-details" }, h("summary", {}, `Other drafts (${alts.length})`),
    h("div", { class: "alts" }, alts.map((alt, k) => ["a", "b"].filter((c) => alt[`${c}_front`]).map((c) => h("div", { class: "alt" },
      h("figure", { class: "fig small" }, casImage(alt[`${c}_front`], { alt: `Other draft ${k + 1} for character ${c.toUpperCase()}` }), h("figcaption", {}, `${c.toUpperCase()} · draft ${k + 2}`)),
      h("button", { type: "button", class: "btn small", onclick: async () => { const r = await decide(gate, tile, "select_alternative", { choice: `${c}:${k}` }); if (r.status === "done") toast(`Using that draft for character ${c.toUpperCase()}.`, { kind: "ok" }); await refresh(); } }, `Use for ${c.toUpperCase()}`)))))) : null;
  const dnaSummary = dna ? h("dl", { class: "dna-mini" },
    [["Theme", dna.world?.theme], ["Pair style", structureLabel(dna.world?.pair_structure || "")], ["Colours", humanize(dna.palette_family || "")],
      ["A: shape style", humanize(dna.a?.shape_language || "")], ["B: shape style", humanize(dna.b?.shape_language || "")],
      ["A: signature object", dna.a?.motif_object], ["B: signature object", dna.b?.motif_object]].filter(([, v]) => v).map(([k, v]) => [h("dt", {}, k), h("dd", {}, v)]))
    : h("p", { class: "muted" }, "The design card is not ready yet.");
  const cardBtn = dna ? h("button", { type: "button", class: "btn small", onclick: () => openCard(dna, gate, tile, refresh, project) }, "See the full design card") : null;
  const levels = Object.entries(facts.critic_levels || {});
  const act = {
    approve: async () => {
      if (notBuildable.length && !ack.checked) { ackNote.hidden = false; ack.focus(); return; }
      ackNote.hidden = true;
      const r = await decide(gate, tile, "approve", { choice: notBuildable.length ? "ack:CON-04" : null, what: `plan ${i + 1}` });
      if (r.status === "done") {
        toast("Concept approved. The final pictures and the parts are being made.", { kind: "ok" });
        ctx.navigate(`/p/${project.id}/board`);
      }
      else await refresh();
    },
    reimagine: async () => {
      const target = await pickScope("Try this plan again", "Pick who gets a fresh idea. The one you keep stays exactly as it is.");
      if (!target) return;
      const r = await decide(gate, tile, "reimagine", { target });
      if (r.status === "done") toast("Drawing a new version. It will appear here.", { kind: "info" });
      await refresh();
    },
    change: () => changeDialog(gate, tile, project.id, refresh),
  };
  return h("article", { class: "plan-tile" + (wildcard ? " wild" : ""), dataset: { tileId: tile.tile_id } },
    h("header", { class: "plan-tile-head" },
      h("h2", {}, `Plan ${i + 1}`, wildcard ? badge("Wildcard", "wild") : null),
      facts.rank != null ? h("span", { class: "muted" }, `Ranked ${Number(facts.rank) + 1} of 3`) : null),
    sheet,
    h("div", { class: "plan-col" },
      wildcard ? h("p", { class: "muted small" }, "The wildcard is the surprise pick: a different direction on purpose.") : null,
      dnaSummary, cardBtn, altBlock,
      levels.length ? h("details", {}, h("summary", {}, "How the judge scored it"), h("dl", { class: "levels" }, levels.map(([k, v]) => [h("dt", {}, humanize(k)), h("dd", {}, humanize(String(v)))]))) : null,
      cover.length ? h("div", { class: "coverage" }, h("h3", {}, "Your must-include lines"), h("ul", {}, cover.map((c) => h("li", {}, badge(c.covered ? "Included" : "Missing", c.covered ? "ok" : "bad"), " ", c.text)))) : null),
    h("div", { class: "plan-col" },
      hardLines.length ? h("div", { class: "hard-fails", role: "note" }, h("p", { class: "hard-title" }, "Needs fixing first"), h("ul", {}, hardLines.map((x) => h("li", {}, x))),
        h("p", { class: "muted" }, "Try Reimagine for the character that failed, or pick another plan.")) : null,
      noteLines.length && !hardLines.length ? h("ul", { class: "muted plain-list" }, noteLines.map((x) => h("li", {}, x))) : null,
      notBuildable.length ? h("div", { class: "not-buildable", role: "group", "aria-label": "Not buildable as drawn" },
        h("h3", {}, "Not buildable as drawn"), h("p", { class: "muted" }, "The picture shows these, but the Roblox parts cannot be built exactly like that. The final build follows the plan, not the painting."),
        h("ul", {}, notBuildable.map((x) => h("li", {}, x))),
        h("label", { class: "ack", for: ackId }, ack, h("span", {}, "I understand this part is built differently")), ackNote) : null,
      warningList(facts.warnings, firstChoice)),
    h("footer", { class: "tile-actions" }, h("div", { class: "row" },
      h("button", { type: "button", class: "btn primary", "data-action": "approve", disabled: hardLines.length > 0, title: hardLines.length ? "A drawing failed a required check. Try Reimagine first." : "Approve this plan", onclick: act.approve }, "Approve"),
      h("button", { type: "button", class: "btn", "data-action": "reimagine", onclick: act.reimagine }, "Reimagine"),
      h("button", { type: "button", class: "btn", "data-action": "change", onclick: act.change }, "Change…"))));
}

/** @param {string} title @param {string} text @returns {Promise<string | undefined>} */
function pickScope(title, text) {
  /** @type {import("../components/modal.js").DialogHandle} */
  let dlg;
  const group = h("fieldset", { class: "choice-group" }, h("legend", {}, "Who should get a new idea?"),
    SCOPES.map((s, k) => h("label", { class: "choice" }, h("input", { type: "radio", name: "scope", value: s.value, checked: k === 0 }), h("span", {}, s.label))));
  const go = h("button", { type: "button", class: "btn primary", onclick: () => dlg.close(/** @type {HTMLInputElement | null} */ (group.querySelector("input:checked"))?.value) }, "Reimagine");
  dlg = openDialog({ title, body: [h("p", {}, text), h("p", { class: "muted" }, "This can cost a little; the cost bar shows what it adds."), group], actions: [h("button", { type: "button", class: "btn", onclick: () => dlg.close() }, "Cancel"), go] });
  go.focus();
  return /** @type {Promise<string | undefined>} */ (dlg.closed);
}

/** @param {any} gate @param {any} tile @param {string} projectId @param {() => Promise<void>} refresh */
function changeDialog(gate, tile, projectId, refresh) {
  /** @type {import("../components/modal.js").DialogHandle} */
  let dlg;
  const box = changeBox({
    title: `Change ${tile.label || "this plan"}`, targets: SCOPES, defaultTarget: "both", targetLabel: "Who does the change apply to?",
    placeholder: "For example: make her jacket teal", maskImageUrl: undefined,
    onSubmit: async ({ text, target }) => {
      const before = (await get("/api/gates", { query: { project_id: projectId, state: "open" } }).catch(() => [])).map((/** @type {any} */ g) => g.id);
      const r = await decide(gate, tile, "change", { text, target });
      if (r.status === "error") throw new Error(r.message);
      dlg.close();
      await refresh();
      if (r.status === "done") await followChange({ projectId, changeId: r.decision?.change_request_id ?? null, afterGateIds: before, onApplied: () => { void refresh(); } });
      await refresh();
    },
    onCancel: () => dlg.close(),
  });
  dlg = openDialog({ title: "Change…", wide: true, body: box });
  box.focusText();
}

/** @param {any} dna @param {any} gate @param {any} tile @param {() => Promise<void>} refresh @param {any} project */
function openCard(dna, gate, tile, refresh, project) {
  /** @type {import("../components/modal.js").DialogHandle} */
  let dlg;
  const card = dnaCard(dna, { highlight: DNA_USED_BY.concept, highlightNote: "used for the picture", onChange: (field) => {
    dlg.close();
    changeDialogFor(gate, tile, project.id, refresh, field);
  } });
  dlg = openDialog({ title: "The design card", wide: true, body: [h("p", { class: "muted" }, "This is the plan in words. Fields marked “used for the picture” are what the drawing follows. Changing one tells you what would be redone before anything runs."), card] });
}

/** @param {any} gate @param {any} tile @param {string} projectId @param {() => Promise<void>} refresh @param {string | null} field */
function changeDialogFor(gate, tile, projectId, refresh, field) {
  /** @type {import("../components/modal.js").DialogHandle} */
  let dlg;
  const box = changeBox({
    title: field ? `Change: ${humanize(field)}` : "Change the design card", targets: SCOPES, defaultTarget: "both", targetLabel: "Who does the change apply to?",
    placeholder: field ? `Say what ${humanize(field).toLowerCase()} should be` : "Say what should be different",
    onSubmit: async ({ text, target }) => {
      const before = (await get("/api/gates", { query: { project_id: projectId, state: "open" } }).catch(() => [])).map((/** @type {any} */ g) => g.id);
      const r = await decide(gate, tile, "change", { text: field ? `${humanize(field)}: ${text}` : text, target });
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
