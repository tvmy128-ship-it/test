// @ts-check
// The smaller gates (APP_SPEC 9.10) as panels that any project page can show on top: BUDGET (Continue once / Raise cap /
// Stop), HUMAN_REVIEW (accept the best so far / Change… / Back to concept) and SETUP_APPROVAL (Approve / Reimagine).
// The CLARIFY and CHANGE_CONFIRM dialogs are changeflow.js (this file only offers the way back to them), MANUAL_IMPORT is the Build page.
import { get, post, friendly } from "../api.js";
import { h, money, humanize } from "../dom.js";
import { decide } from "./decisions.js";
import { panel } from "./ui.js";
import { confirmDialog } from "./modal.js";
import { figure } from "./tile.js";
import { runTileAction } from "./tile-actions.js";
import { followChange } from "./changeflow.js";
import { toast } from "./toast.js";
import { stepLabel } from "../text.js";

const REASONS = /** @type {Record<string, string>} */ ({
  over_cap: "This step would take the duo past its budget cap.",
  ask_above: "This step costs more than the amount you asked to be asked about.",
  daily_cap: "This would pass your daily limit.",
  tripo_credits: "There may not be enough Tripo credits for this step.",
});

/** @param {any} gate @param {() => Promise<void> | void} refresh */
function budgetPanel(gate, refresh) {
  const tile = gate.tiles[0];
  const f = tile.facts || {};
  const est = Number(f.estimate_usd ?? 0);
  const cap = Number(f.cap_usd ?? 0);
  const newCap = h("input", { type: "number", min: "1", step: "0.5", value: String(Math.ceil((Number(f.spent_usd ?? 0) + est) + 1)), "aria-label": "New cap in dollars", class: "narrow" });
  /** @param {string} action @param {string} [choice] */
  const send = async (action, choice) => {
    const r = await decide(gate, tile, action, { choice });
    if (r.status === "done") toast(action === "stop" ? "Stopped. Nothing was charged for this step." : "Going ahead.", { kind: action === "stop" ? "info" : "ok" });
    await refresh();
  };
  return panel({ class: "gate-panel budget", title: "This step costs more than usual. Your call." },
    h("p", {}, `${stepLabel(String(f.step_kind || "this step"))} would cost about `, h("strong", {}, money(est)), ". Nothing has been charged for it yet."),
    h("p", { class: "muted" }, REASONS[String(f.reason || "")] ?? "", ` You have spent ${money(Number(f.spent_usd ?? 0))} of the ${money(cap)} cap on this duo.`),
    typeof f.tripo_credits_available === "number" ? h("p", { class: "muted" }, `Tripo credits available: ${f.tripo_credits_available}`) : null,
    h("div", { class: "row" },
      h("button", { type: "button", class: "btn primary", onclick: () => send("continue") }, "Continue once"),
      h("span", { class: "inline-field" }, h("label", {}, "Or raise the cap to $"), newCap, h("button", { type: "button", class: "btn", onclick: () => send("raise_cap", newCap.value) }, "Raise the cap")),
      h("button", { type: "button", class: "btn danger", onclick: async () => { if (await confirmDialog({ title: "Stop this step?", message: "The step is cancelled and nothing is charged. You can retry it later.", confirmLabel: "Stop it", tone: "danger" })) await send("stop"); } }, "Stop")));
}

/** @param {any} gate @param {string} projectId @param {() => Promise<void> | void} refresh @param {(p: string) => void} navigate */
function humanReviewPanel(gate, projectId, refresh, navigate) {
  const tile = gate.tiles[0];
  const f = tile.facts || {};
  const fails = /** @type {any[]} */ (f.hard_failures || f.failing_checks || []);
  const roles = Object.entries(/** @type {Record<string, string>} */ (tile.assets || {})).slice(0, 4);
  const env = { projectId, gate, tile, refresh, navigate };
  return panel({ class: "gate-panel review", title: `${tile.label || "A part"} needs your eye` },
    h("p", {}, "The automatic fixes ran out of ideas for this one. This is the best version so far."),
    roles.length ? h("div", { class: "strip" }, roles.map(([role, sha]) => figure(sha, role, { small: true }))) : null,
    fails.length ? h("ul", { class: "hard-fails" }, fails.map((x) => h("li", {}, typeof x === "string" ? x : String(x.evidence || x.message || x.check_id)))) : null,
    f.tried?.length ? h("p", { class: "muted" }, "Tried so far: ", f.tried.map(humanize).join(", ")) : null,
    typeof f.cost_usd === "number" ? h("p", { class: "muted" }, `Spent on this part: ${money(f.cost_usd)}`) : null,
    h("div", { class: "row" },
      h("button", { type: "button", class: "btn primary", disabled: f.can_accept === false, title: f.can_accept === false ? "A required safety check is still failing, so this cannot be accepted." : "Use the best version so far", onclick: () => runTileAction(env, "approve") }, "Accept the best so far"),
      h("button", { type: "button", class: "btn", onclick: () => runTileAction(env, "change") }, "Change…"),
      h("button", { type: "button", class: "btn", onclick: async () => { const r = await decide(gate, tile, "back_to_concept"); if (r.status === "done") navigate(`/p/${projectId}/gate1`); await refresh(); } }, "Back to concept")));
}

/** @param {any} gate @param {string} projectId @param {() => Promise<void> | void} refresh @param {(p: string) => void} navigate */
function setupPanel(gate, projectId, refresh, navigate) {
  return panel({ class: "gate-panel setup", title: "Setup picks" },
    h("p", {}, "Pick the examples that match the look you like. These set the house style for every duo."),
    h("div", { class: "grid tiles" }, gate.tiles.map((/** @type {any} */ t) => {
      const env = { projectId, gate, tile: t, refresh, navigate };
      return h("article", { class: "tile" }, h("h3", {}, t.label), h("div", { class: "strip" }, Object.entries(/** @type {Record<string, string>} */ (t.assets || {})).map(([r, s]) => figure(s, r, { small: true }))),
        h("div", { class: "row" }, h("button", { type: "button", class: "btn primary", onclick: () => runTileAction(env, "approve") }, "Approve"), h("button", { type: "button", class: "btn", onclick: () => runTileAction(env, "reimagine") }, "Reimagine")));
    })));
}

/**
 * A change that is waiting on the user (CLARIFY: one question; CHANGE_CONFIRM: the diff and the estimate). The dialogs of changeflow.js open on
 * their own right after "Change…", but a reload, another tab or closing the "working" dialog leaves the gate open with nothing on screen:
 * this panel is the way back to it.
 * @param {any} gate @param {string} projectId @param {() => Promise<void> | void} refresh
 */
function changePanel(gate, projectId, refresh) {
  const f = gate.tiles?.[0]?.facts || {};
  const question = gate.kind === "clarify";
  return panel({ class: "gate-panel change-waiting", title: question ? "A quick question about your change" : "Your change is waiting for your OK" },
    h("p", {}, question ? String(f.question || "The planner needs one more detail before it can work out your change.") : (f.understood_as ? `We understood it as: ${f.understood_as}` : "We worked out what your change would do.")),
    h("p", { class: "muted" }, question ? "Nothing has been redone or charged yet." : "Nothing has been redone or charged yet. You see exactly what would change before you confirm."),
    h("div", { class: "row" }, h("button", { type: "button", class: "btn primary", onclick: async () => {
      await followChange({ projectId, changeId: f.change_id ?? null, afterGateIds: [], onApplied: () => { void refresh(); } });
      await refresh();
    } }, question ? "Answer the question" : "Review the change")));
}

/**
 * Steps of this duo that failed, with the plain-English reason and "Try again". A page that waits for a job ("The parts are being made") would
 * otherwise sit there for ever when the job failed: this is the panel that says so. Null when nothing failed (or the jobs cannot be read).
 * @param {string} projectId @param {() => Promise<void> | void} refresh @param {AbortSignal} [signal]
 * @returns {Promise<HTMLElement | null>}
 */
export async function failedStepsPanel(projectId, refresh, signal) {
  /** @type {any[]} */ let jobs;
  try { jobs = await get("/api/jobs", { query: { project_id: projectId, steps: "true", limit: 20 }, signal }); } catch { return null; }
  // a finished or cancelled job's failures were dealt with (retried, or the job was dropped on purpose)
  const failed = jobs.filter((j) => ["running", "failed", "waiting_user", "paused"].includes(j.job?.state))
    .flatMap((j) => /** @type {any[]} */ (j.steps || []).filter((s) => s.state === "failed" && !String(s.kind).startsWith("export.")));     // the Export page explains its own
  if (!failed.length) return null;
  return panel({ class: "gate-panel failed-steps", title: failed.length === 1 ? "One step did not work" : `${failed.length} steps did not work` },
    h("p", {}, "Your approved work is kept. You can try again; if it keeps failing, the details are in the log file (Settings, Diagnostics)."),
    h("ul", { class: "plain-list" }, failed.slice(0, 5).map((s) => h("li", {},
      h("strong", {}, stepLabel(String(s.kind))), s.part_id ? h("span", { class: "muted" }, ` · ${s.part_id.toUpperCase().replace(/\./g, " ")}`) : null, ": ",
      s.error?.user_hint || "This step did not work.", " ",
      h("button", { type: "button", class: "btn small", onclick: async (/** @type {Event} */ ev) => {
        const b = /** @type {HTMLButtonElement} */ (ev.currentTarget);
        b.disabled = true;
        try { await post(`/api/steps/${s.id}/retry`); toast("Trying again.", { kind: "ok" }); await refresh(); } catch (err) { toast(friendly(err), { kind: "bad" }); b.disabled = false; }
      } }, "Try again")))));
}

/**
 * The panels for the open gates of these kinds, newest last.
 * @param {string} projectId @param {any[]} gates open gates of the project
 * @param {{refresh: () => Promise<void> | void, navigate: (p: string) => void}} o
 */
export function openGatePanels(projectId, gates, o) {
  /** @type {HTMLElement[]} */
  const out = [];
  for (const g of gates) {
    if (g.kind === "budget") out.push(budgetPanel(g, o.refresh));
    else if (g.kind === "human_review") out.push(humanReviewPanel(g, projectId, o.refresh, o.navigate));
    else if (g.kind === "setup_approval") out.push(setupPanel(g, projectId, o.refresh, o.navigate));
    else if (g.kind === "change_confirm" || g.kind === "clarify") out.push(changePanel(g, projectId, o.refresh));
    else if (g.kind === "manual_import") {
      out.push(panel({ class: "gate-panel manual", title: "Waiting for your 3D file" },
        h("p", {}, `${g.tiles?.[0]?.label || "A part"} is waiting for a model you make on Tripo's website. The Build page has the pack folder and a place to drop the file.`),
        h("a", { class: "btn primary", href: `#/p/${projectId}/build` }, "Open the Build page")));
    }
  }
  return out;
}
