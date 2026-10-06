// @ts-check
// Home (#/): start and resume. Project cards with the next thing to do, a clear "New duo" button, and the promise.
import { get, post, friendly } from "../api.js";
import { h, money, comboLabel, timeAgo, setChildren } from "../dom.js";
import { state, subscribe, refreshSnapshot } from "../store.js";
import { pageHeader, panel, badge, emptyState, note } from "../components/ui.js";
import { confirmDialog } from "../components/modal.js";
import { toast } from "../components/toast.js";
import { nextAction, stageSentence, duoPicked, stageIndex, STAGES } from "../text.js";

/** @param {import("../router.js").PageContext} ctx */
export async function render(ctx) {
  const root = ctx.root;
  /** @type {any[] | null} */
  let keys = null;
  try { keys = Object.values(await get("/api/keys", { signal: ctx.signal })); } catch { keys = null; }
  if (!ctx.active()) return;

  const archive = async (/** @type {any} */ p) => {
    const ok = await confirmDialog({ title: `Hide “${p.name}”?`, message: "The duo disappears from this list. Nothing that was approved or exported is deleted.", confirmLabel: "Hide it" });
    if (!ok) return;
    try { await post(`/api/projects/${p.id}/archive`); toast("Hidden.", { kind: "ok" }); await refreshSnapshot(); } catch (err) { toast(friendly(err), { kind: "bad" }); }
  };

  /** @param {any} p */
  const card = (p) => {
    const gates = state.snapshot?.open_gates.filter((g) => g.project_id === p.id) ?? [];
    const na = nextAction(p, gates);
    const waiting = p.waiting_on_user > 0;
    const idx = stageIndex(p.stage);
    return h("article", { class: "project-card" + (waiting ? " needs-you" : ""), dataset: { projectId: p.id } },
      h("div", { class: "pc-head" },
        h("h3", {}, h("a", { href: `#/p/${p.id}` }, p.name)),
        waiting ? badge(`${p.waiting_on_user} waiting on you`, "info") : null,
        p.paused ? badge("Paused", "warn") : null),
      h("p", { class: "pc-meta" }, comboLabel(p.combo), " · ", STAGES[idx].label, p.stage === "exported" ? " (finished)" : ""),
      h("p", { class: "pc-stage" }, stageSentence(p.stage, duoPicked(p, gates))),
      h("p", { class: "pc-cost muted" }, `Spent: ${money(p.spent_usd)} · Updated ${timeAgo(p.updated_at)}`),
      h("div", { class: "pc-actions" },
        h("a", { class: "btn " + (waiting || p.stage === "brief" ? "primary" : ""), href: na.href }, na.label),
        h("button", { type: "button", class: "btn quiet small", onclick: () => archive(p), "aria-label": `Hide ${p.name}` }, "Hide")));
  };

  const list = h("div", { class: "project-grid" });
  const nudge = h("div", {});
  const doctorLine = h("p", { class: "doctor-line" });
  const paint = () => {
    const projects = /** @type {any[]} */ (state.snapshot?.projects ?? []);
    const sorted = [...projects].sort((a, b) => (b.waiting_on_user - a.waiting_on_user) || String(b.updated_at).localeCompare(String(a.updated_at)));
    setChildren(list, ...(sorted.length ? sorted.map(card) : [emptyState("No duos yet", "A duo is two matching Roblox characters. Tell us the idea in a few words and we will make three plans for you to choose from.",
      h("a", { class: "btn primary big", href: "#/new" }, "Start your first duo"))]));
    const doc = state.snapshot?.doctor;
    setChildren(doctorLine, "Setup check: ", !doc ? badge("not run yet", "muted") : doc.blocks_paid_features ? badge("needs attention", "bad") : (doc.summary?.warnings ?? 0) > 0 ? badge(`${doc.summary.warnings} to look at`, "warn") : badge("all good", "ok"), " ", h("a", { href: "#/setup" }, "Open setup"));
    const demo = Boolean(state.health?.demo ?? state.snapshot?.demo);
    const missing = keys?.filter((k) => k.requirement === "required" && !k.set) ?? [];
    setChildren(nudge, ...(!demo && missing.length
      ? [panel({ class: "nudge" }, h("h2", {}, "First time here?"), h("p", {}, `${missing.length === 1 ? "One key is" : "A few keys are"} still missing, so real pictures cannot be made yet. You can set them up, or try everything in demo mode with practice pictures and no cost.`),
        h("div", { class: "row" }, h("a", { class: "btn primary", href: "#/setup" }, "Set up DuoSkin Studio"), h("a", { class: "btn", href: "#/settings/providers" }, "Try demo mode")))]
      : []));
  };
  paint();
  ctx.onCleanup(subscribe(paint));

  setChildren(root, 
    pageHeader({ title: "Your duos", lead: "A duo is two matching Roblox characters. You decide at every step: nothing expensive runs before you approve it.",
      actions: h("a", { class: "btn primary big", href: "#/new" }, "New duo") }),
    nudge, doctorLine,
    h("section", { "aria-label": "Your duos" }, list),
    h("section", { class: "promise" }, h("h2", {}, "How it works"),
      h("ol", { class: "steps-list" },
        h("li", {}, h("strong", {}, "You describe the idea."), " We write three plans."),
        h("li", {}, h("strong", {}, "You pick one."), " Then every part (face, hair, clothes, accessories) is shown alone."),
        h("li", {}, h("strong", {}, "Each part has three buttons:"), " Approve, Reimagine (try again) or Change… (say what to change)."),
        h("li", {}, h("strong", {}, "Only approved parts get built."), " The cost bar at the top always shows what you have spent against your cap."))),
    h("section", { class: "quick-links", "aria-label": "More" },
      h("a", { class: "link-card", href: "#/library" }, h("strong", {}, "Library"), h("span", {}, "Hair styles, head bases, fabrics and the things you have collected")),
      h("a", { class: "link-card", href: "#/calibration" }, h("strong", {}, "Calibration"), h("span", {}, "Quick blind rounds that teach the checks your taste")),
      h("a", { class: "link-card", href: "#/learning" }, h("strong", {}, "Learning"), h("span", {}, "A weekly report on what is working"))),
    state.offline ? note(state.error, "bad") : null);
}
