// @ts-check
// Project hub (#/p/<id>): the brief as saved, what is waiting on you, and the one next action.
import { get, post, patch, friendly, ApiError, casImage } from "../api.js";
import { h, money, comboLabel, timeAgo, setChildren } from "../dom.js";
import { pageHeader, panel, badge, keyValues, note, errorNote } from "../components/ui.js";
import { confirmDialog, openDialog } from "../components/modal.js";
import { toast } from "../components/toast.js";
import { nextAction, stageSentence, duoPicked, structureLabel, gateKindLabel } from "../text.js";
import { openGatePanels } from "../components/gatepanels.js";

/** @param {import("../router.js").PageContext} ctx */
export async function render(ctx) {
  const id = ctx.params.id;
  const draw = async () => {
    /** @type {any} */ let bundle;
    try { bundle = await get(`/api/projects/${encodeURIComponent(id)}`, { signal: ctx.signal }); } catch (err) {
      if (!ctx.active()) return;
      setChildren(ctx.root, pageHeader({ title: "That duo was not found", back: { href: "#/", label: "Your duos" } }), errorNote(friendly(err)));
      return;
    }
    if (!ctx.active()) return;
    const p = bundle.project;
    ctx.setTitle(p.name);
    const gates = /** @type {any[]} */ (bundle.open_gates || []);
    const na = nextAction(p, gates);
    const picked = duoPicked(p, gates);
    const startPlan = h("button", { type: "button", class: "btn primary big" }, "Start the plan");
    startPlan.addEventListener("click", async () => {
      startPlan.disabled = true;
      try { await post(`/api/projects/${id}/plan`); ctx.navigate(`/p/${id}/plan`); } catch (err) {
        toast(err instanceof ApiError && err.unavailable ? "The planning part is not available yet in this build, so the duo stays at the brief." : friendly(err), { kind: err instanceof ApiError && err.unavailable ? "warn" : "bad" });
        startPlan.disabled = false;
      }
    });
    const refresh = async () => { await draw(); };
    const next = panel({ class: "next-card" },
      h("h2", {}, "What now?"),
      h("p", { class: "next-sentence" }, stageSentence(p.stage, picked)),
      p.stage === "brief" ? startPlan : h("a", { class: "btn primary big", href: na.href }, na.label),
      p.stage === "brief" ? h("p", { class: "muted" }, "Planning usually costs about $2 to $4.50 (an estimate). You approve one of three plans before anything bigger runs.") : null);
    const gateList = gates.length
      ? panel({ title: "Waiting on you" }, h("ul", { class: "plain-list" }, gates.map((g) => h("li", {}, badge(gateKindLabel(g.kind), "info"), " ", g.tiles?.length ? `${g.tiles.length} item${g.tiles.length === 1 ? "" : "s"}` : ""))))
      : null;
    const refs = /** @type {any[]} */ (p.references || []);
    const pauseBtn = h("button", { type: "button", class: "btn", onclick: async () => {
      try { await post(`/api/projects/${id}/${p.paused ? "resume" : "pause"}`); toast(p.paused ? "Resumed." : "Paused. Running steps finish, nothing new starts.", { kind: "ok" }); await refresh(); } catch (err) { toast(friendly(err), { kind: "bad" }); }
    } }, p.paused ? "Resume this duo" : "Pause this duo");
    const nameBtn = h("button", { type: "button", class: "btn", onclick: () => {
      const input = h("input", { type: "text", id: "rename-input", maxlength: "60", value: p.name });
      /** @type {import("../components/modal.js").DialogHandle} */
      let dlg;
      const save = h("button", { type: "button", class: "btn primary", onclick: async () => {
        const name = input.value.trim();
        if (!name || name === p.name) { dlg.close(); return; }
        try { await patch(`/api/projects/${id}`, { expected_version: p.version, name }); dlg.close(); await refresh(); } catch (err) { toast(friendly(err), { kind: "bad" }); }
      } }, "Save the name");
      input.addEventListener("keydown", (e) => { if (e.key === "Enter") save.click(); });
      dlg = openDialog({ title: "Rename this duo", body: [h("label", { for: "rename-input" }, "Name"), input], actions: [h("button", { type: "button", class: "btn", onclick: () => dlg.close() }, "Cancel"), save] });
      input.select();
    } }, "Rename");
    const archiveBtn = h("button", { type: "button", class: "btn quiet", onclick: async () => {
      if (!(await confirmDialog({ title: `Hide “${p.name}”?`, message: "Nothing approved or exported is deleted.", confirmLabel: "Hide it" }))) return;
      try { await post(`/api/projects/${id}/archive`); ctx.navigate("/"); } catch (err) { toast(friendly(err), { kind: "bad" }); }
    } }, "Hide this duo");
    setChildren(ctx.root, 
      pageHeader({ title: p.name, lead: `${comboLabel(p.combo)} · started ${timeAgo(p.created_at)} · spent ${money(p.spent_usd)} of your ${money(p.settings.budget_usd)} cap`, back: { href: "#/", label: "Your duos" } }),
      ...openGatePanels(id, gates, { refresh, navigate: ctx.navigate }),
      next, gateList,
      panel({ title: "The brief" },
        p.brief ? h("blockquote", { class: "brief-quote" }, p.brief) : h("p", { class: "muted" }, "No text brief: the planner chooses the idea."),
        keyValues([
          ["Pair", comboLabel(p.combo)],
          ["How the two relate", p.structure_request === "auto" ? "Let the planner choose" : structureLabel(p.structure_request)],
          ["Must include", p.must_include?.length ? h("ul", { class: "plain-list" }, p.must_include.map((/** @type {string} */ m) => h("li", {}, m))) : "Nothing in particular"],
          ["Budget cap", money(p.settings.budget_usd)],
          ["Asks before a step over", money(p.settings.ask_above_usd)],
          ["3D parts", { ask: "Ask me when it starts", api: "Tripo makes them", manual: "I make them on Tripo" }[/** @type {"ask"|"api"|"manual"} */ (p.settings.mesh_mode)] ?? p.settings.mesh_mode],
          ["Compare with my reference", p.settings.reference_similarity_check ? "On" : "Off"],
        ]),
        refs.length ? h("ul", { class: "ref-grid" }, refs.map((r, i) => h("li", {}, casImage(r.asset_sha, { alt: `Reference picture ${i + 1}` })))) : null),
      panel({ title: "Manage" }, h("div", { class: "row" }, pauseBtn, nameBtn, archiveBtn)),
      p.stage !== "brief" ? note("The stage bar at the top takes you to any step you have already reached.", "info") : null);
  };
  await draw();
  ctx.live(["project.stage", "gate.opened", "gate.updated", "cost.added"], () => { void draw(); });
}
