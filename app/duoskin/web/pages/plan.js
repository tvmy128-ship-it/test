// @ts-check
// Plan (#/p/<id>/plan): watch the plan loop. Step timeline, the planner's thinking summary, lint results per plan
// (HARD always; SOFT only after Gate 1's first choice), critic levels, revisions and the concept-draft progress.
import { get, tryGet, post, friendly, ApiError } from "../api.js";
import { h, humanize, setChildren } from "../dom.js";
import { pageHeader, panel, badge, progress, emptyState, errorNote, notAvailable } from "../components/ui.js";
import { openGatePanels } from "../components/gatepanels.js";
import { toast } from "../components/toast.js";
import { stepLabel, stepStateLabel, planPhase, structureLabel } from "../text.js";
import { diffList } from "../components/diff.js";
import { state as store } from "../store.js";
import { valueText } from "../components/diff.js";

/** @param {string} state */
function stateTone(state) {
  return state === "succeeded" ? "ok" : state === "failed" ? "bad" : state === "running" || state === "waiting_remote" ? "busy" : state === "waiting_user" ? "info" : "muted";
}

/** @param {import("../router.js").PageContext} ctx */
export async function render(ctx) {
  const id = ctx.params.id;
  let thinking = "";
  const thinkingBox = h("p", { class: "thinking muted" });
  const paintThinking = () => { thinkingBox.textContent = thinking || "The planner's notes appear here while it works."; };
  paintThinking();
  ctx.on("llm.thinking", (e) => { if (e.project_id === id) { thinking = String(e.payload.text || e.payload.summary || thinking); paintThinking(); } });

  // recent thinking text, if the plan started before this page opened
  try {
    const snap = await get("/api/state", { signal: ctx.signal });
    const poll = await get("/api/events/poll", { query: { after: Math.max(0, (snap.max_event_id || 0) - 400), limit: 500 }, signal: ctx.signal });
    const last = /** @type {any[]} */ (poll.events || []).filter((e) => e.type === "llm.thinking" && e.project_id === id).pop();
    if (last) { thinking = String(last.payload.text || last.payload.summary || ""); paintThinking(); }
  } catch { /* the notes are optional */ }
  if (!ctx.active()) return;

  const body = h("div", {});
  setChildren(ctx.root, pageHeader({ title: "The plan", lead: "Three plans are being written and checked. Nothing here costs more than the small planning steps; you pick one next.", back: { href: `#/p/${id}`, label: "Your duo" } }), body);

  const draw = async () => {
    /** @type {any} */ let bundle; /** @type {any[]} */ let jobs = []; let specs = { data: /** @type {any[]} */ ([]), unavailable: false };
    let firstChoice = false;
    try {
      [bundle, jobs] = await Promise.all([get(`/api/projects/${encodeURIComponent(id)}`, { signal: ctx.signal }), get("/api/jobs", { query: { project_id: id, steps: "true" }, signal: ctx.signal })]);
      specs = await tryGet(`/api/projects/${encodeURIComponent(id)}/specs`, { signal: ctx.signal });
      const decided = await tryGet("/api/gates", { query: { project_id: id, state: "decided" } });
      firstChoice = [...(decided.data || []), ...(bundle.open_gates || [])].some((/** @type {any} */ g) => g.kind === "concept" && g.first_choice_at);
    } catch (err) {
      if (ctx.active()) setChildren(body, errorNote(friendly(err)));
      return;
    }
    if (!ctx.active()) return;
    const gates = /** @type {any[]} */ (bundle.open_gates || []);
    const p = bundle.project;
    const planJob = [...jobs].reverse().find((j) => j.job.kind === "plan") ?? null;
    const steps = /** @type {any[]} */ (planJob?.steps ?? []);
    const concept = gates.find((g) => g.kind === "concept");

    const parts = [];
    parts.push(...openGatePanels(id, gates.filter((g) => g.kind !== "concept"), { refresh: draw, navigate: ctx.navigate }));
    if (concept) {
      parts.push(panel({ class: "next-card" }, h("h2", {}, "Your concepts are ready"), h("p", { class: "next-sentence" }, "Three plans were drawn. Pick the one you like, or ask for changes."),
        h("a", { class: "btn primary big", href: `#/p/${id}/gate1` }, "Pick a concept")));
    } else if (p.stage === "brief") {
      const go = h("button", { type: "button", class: "btn primary big", onclick: async () => {
        go.disabled = true;
        try { await post(`/api/projects/${id}/plan`); await draw(); } catch (err) { toast(err instanceof ApiError && err.unavailable ? "The planning part is not available yet in this build." : friendly(err), { kind: "warn" }); go.disabled = false; }
      } }, "Start the plan");
      parts.push(panel({}, h("h2", {}, "The plan has not started"), go));
    }
    // timeline: consecutive steps of the same kind are one row ("Judging each plan, 3 of 3")
    /** @type {{kind: string, steps: any[]}[]} */
    const groups = [];
    for (const st of steps.filter((x) => x.state !== "superseded")) {
      const last = groups[groups.length - 1];
      if (last && last.kind === st.kind) last.steps.push(st); else groups.push({ kind: st.kind, steps: [st] });
    }
    const demo = Boolean(store.health?.demo);
    const stepRows = groups.length ? groups.map((g) => {
      const list = g.steps;
      const failed = list.find((x) => x.state === "failed");
      const running = list.find((x) => ["running", "waiting_remote"].includes(x.state));
      const waitingUser = list.find((x) => x.state === "waiting_user");
      const done = list.filter((x) => x.state === "succeeded").length;
      const state = failed ? "failed" : running ? running.state : waitingUser ? "waiting_user" : done === list.length ? "succeeded" : done ? "running" : list[0].state;
      const shown = failed ?? running ?? list[list.length - 1];
      const paid = list.some((x) => x.paid) && !demo;
      return h("li", { class: `step ${state}` },
        h("span", { class: "step-phase muted" }, planPhase(g.kind)),
        h("span", { class: "step-main" }, h("strong", {}, stepLabel(g.kind)), list.length > 1 ? h("span", { class: "muted" }, ` · ${done} of ${list.length} done`) : null, paid ? h("span", { class: "muted" }, " · uses a paid service") : null),
        badge(stepStateLabel(state), /** @type {any} */ (stateTone(state))),
        running ? progress(running.progress || 0, stepLabel(g.kind)) : null,
        shown.message && !failed ? h("span", { class: "step-msg muted" }, shown.message) : null,
        failed ? h("span", { class: "step-error" }, failed.error?.user_hint || "This step did not work.", " ",
          h("button", { type: "button", class: "btn small", onclick: async () => { try { await post(`/api/steps/${failed.id}/retry`); await draw(); } catch (e) { toast(friendly(e), { kind: "bad" }); } } }, "Try again")) : null);
    }) : [emptyState("Nothing has run yet", p.stage === "brief" ? "Start the plan to see the steps here." : "The first steps appear in a moment.")];
    const drafts = steps.filter((s) => /^img\./.test(s.kind) || s.kind.startsWith("concept."));
    const doneDrafts = drafts.filter((s) => s.state === "succeeded").length;
    parts.push(panel({ title: "What is happening" }, h("ol", { class: "timeline" }, stepRows),
      drafts.length ? h("p", { class: "muted" }, `Concept drawings: ${doneDrafts} of ${drafts.length} steps done.`) : null,
      h("div", { class: "thinking-box" }, h("h3", {}, "The planner's notes"), thinkingBox)));

    // plans so far
    const specRows = /** @type {any[]} */ (specs.data || []);
    if (specs.unavailable) parts.push(panel({ title: "The plans" }, notAvailable("The list of plans")));
    else if (specRows.length) {
      parts.push(panel({ title: "The plans so far", lead: firstChoice ? "Required rules are checked for every plan, and a few suggestions are shown now that you have made your first choice." : "Every plan is checked against the required rules. Suggestions only appear after your first choice." },
        h("div", { class: "plan-grid" }, specRows.map((row, i) => planCard(row, i, firstChoice)))));
    }
    setChildren(body, ...parts);
  };
  await draw();
  ctx.live(["step.state", "step.progress", "job.state", "gate.opened", "gate.updated", "project.stage", "spec.updated"], () => { void draw(); }, 600);
}

/** @param {any} row @param {number} i @param {boolean} firstChoice */
function planCard(row, i, firstChoice) {
  const rec = row.spec;
  const spec = rec.spec || {};
  const world = spec.world || {};
  const lint = /** @type {any[]} */ (row.lint || []);
  const hardFail = lint.filter((c) => (c.kind === "hard" || c.kind === "assert") && c.passed === false);
  const hardTotal = lint.filter((c) => c.kind === "hard" || c.kind === "assert").length;
  const soft = firstChoice ? lint.filter((c) => c.kind === "soft" && c.passed === false).slice(0, 2) : [];
  const levels = Object.entries(rec.critic_levels || {});
  const patches = /** @type {any[]} */ (rec.patch_from_parent || []);
  return h("article", { class: "plan-card" },
    h("h3", {}, `Plan ${i + 1}`, spec.is_wildcard ? badge("Wildcard", "wild") : null, rec.status === "dropped" ? badge("Not shown", "muted") : null),
    world.theme ? h("p", { class: "plan-theme" }, world.theme) : h("p", { class: "muted" }, "Still being written"),
    world.pair_structure ? h("p", { class: "muted" }, `Pair style: ${structureLabel(world.pair_structure)}`) : null,
    hardTotal || lint.length ? h("p", {}, hardFail.length ? badge(`${hardFail.length} rule${hardFail.length === 1 ? "" : "s"} to fix`, "bad") : badge("Rules check passed", "ok")) : h("p", { class: "muted" }, "Rules not checked yet"),
    hardFail.length ? h("ul", { class: "hard-fails" }, hardFail.map((c) => h("li", {}, c.evidence || humanize(c.check_id)))) : null,
    soft.length ? h("div", { class: "heads-up" }, h("p", { class: "heads-up-title" }, "Suggestions"), h("ul", {}, soft.map((c) => h("li", {}, c.evidence || humanize(c.check_id))))) : null,
    levels.length ? h("div", {}, h("p", { class: "muted" }, levelSummary(levels)), h("details", {}, h("summary", {}, "How the judge scored it"), h("dl", { class: "levels" }, levels.map(([k, v]) => [h("dt", {}, humanize(k)), h("dd", {}, humanize(String(v)))])))) : null,
    rec.rank != null ? h("p", { class: "muted" }, `Rank: ${rec.rank + 1}`) : null,
    patches.length ? h("details", {}, h("summary", {}, `Improved ${patches.length} time${patches.length === 1 ? "" : "s"}`), diffList(patches.map((p) => ({ path: p.path, old: "", new: safeParse(p.value_json) })), { empty: "" })) : null,
    null);
}

/** "6 strong, 4 fine, 1 weak" from the critic's levels. @param {[string, any][]} levels */
function levelSummary(levels) {
  /** @type {Record<string, number>} */
  const n = {};
  for (const [, v] of levels) n[String(v).toLowerCase()] = (n[String(v).toLowerCase()] ?? 0) + 1;
  return ["strong", "ok", "weak"].filter((k) => n[k]).map((k) => `${n[k]} ${k === "ok" ? "fine" : k}`).join(", ");
}

/** @param {string} s */
function safeParse(s) { try { return valueText(JSON.parse(s)); } catch { return s; } }
