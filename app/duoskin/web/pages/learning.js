// @ts-check
// Learning (#/learning; APP_SPEC 3.9): the weekly report, regression runs (always with an estimate and a confirmation),
// the variety guard and version promotion. All numbers come from the report; the page only explains them.
import { get, post, tryGet, friendly, ApiError } from "../api.js";
import { h, humanize, money, setChildren } from "../dom.js";
import { pageHeader, panel, field, note, notAvailable, errorNote, emptyState } from "../components/ui.js";
import { renderReport } from "../components/report.js";
import { confirmDialog } from "../components/modal.js";
import { toast } from "../components/toast.js";

const LABELS = /** @type {Record<string, string>} */ ({
  flag_rate_on_approved: "How often it warned about duos you approved", catch_rate_on_rejected: "How often it warned about duos you rejected",
  gate1_first_try_approval: "Concepts approved on the first try", wildcard_pick_rate: "How often you picked the wildcard", cost_per_duo: "Cost per duo",
  hard_reject_rate_on_approved: "Required checks that rejected a duo you approved", registry_reject_rate: "Designs rejected for being too alike an earlier one",
  nearest_duo_distances: "How different each duo is from the previous ones", structure_use: "How often each pair style was used",
});

/** @param {import("../router.js").PageContext} ctx */
export async function render(ctx) {
  const body = h("div", {});
  setChildren(ctx.root, pageHeader({ title: "Learning", lead: "A weekly look at what is working: which checks help, how much a duo costs, and how varied your duos are." }), body);

  const draw = async () => {
    /** @type {{data: any, unavailable: boolean}} */ let rep; /** @type {any} */ let settings = null;
    try { rep = await tryGet("/api/learning/report", { signal: ctx.signal }); settings = await get("/api/settings", { signal: ctx.signal }); } catch (err) { if (ctx.active()) setChildren(body, errorNote(friendly(err))); return; }
    if (!ctx.active()) return;
    const cands = Object.entries(settings.models?.candidates ?? {});
    // regression
    const stage = h("select", { "aria-label": "Which part to test" }, [["plan", "Plans and concept pictures"], ["parts", "Part pictures"]].map(([v, t]) => h("option", { value: v }, t)));
    const sample = h("select", { "aria-label": "How many briefs" }, [["10", "A sample of 10 briefs (cheaper)"], ["40", "All 40 briefs"]].map(([v, t]) => h("option", { value: v }, t)));
    const regErr = h("div", { class: "msg" });
    const run = h("button", { type: "button", class: "btn", onclick: async () => {
      setChildren(regErr);
      const est = sample.value === "40" ? "about $80 to $180" : "about $20 to $45";
      if (!(await confirmDialog({ title: "Run the regression test?", message: `This checks that new versions are not worse than the current ones. It costs ${est} (an estimate). DuoSkin shows the exact estimate and asks again before anything is charged.`, confirmLabel: "Show me the estimate" }))) return;
      try {
        await post("/api/regression/run", { stage: stage.value, sample: Number(sample.value), candidate_versions: Object.fromEntries(cands) });
        toast("Started. A cost confirmation will appear on the Jobs page before anything runs.", { kind: "ok" });
      } catch (e) { setChildren(regErr, e instanceof ApiError && e.unavailable ? note("The regression test is not available yet in this build.", "warn") : note(friendly(e), "bad")); }
    } }, "Start the regression test");
    // promotion
    const role = h("select", { "aria-label": "Which version" }, cands.length ? cands.map(([r, v]) => h("option", { value: `${r}|${v}` }, `${humanize(r)}: ${v}`)) : [h("option", { value: "" }, "No candidates waiting")]);
    const promoErr = h("div", { class: "msg" });
    const promote = h("button", { type: "button", class: "btn", disabled: !cands.length, onclick: async () => {
      const [r, v] = role.value.split("|");
      setChildren(promoErr);
      try { await post("/api/versions/promote", { role: r, version: v }); toast("Promoted. New duos will use it.", { kind: "ok" }); await draw(); } catch (e) { setChildren(promoErr, e instanceof ApiError && e.unavailable ? note("Promotion is not available yet in this build.", "warn") : note(e instanceof ApiError && e.status >= 400 && e.status < 500 ? `Not yet: ${friendly(e)} A version is only promoted after the latest regression test passed.` : friendly(e), "warn")); }
    } }, "Promote this version");

    setChildren(body, 
      rep.unavailable ? panel({ title: "This week" }, notAvailable("The weekly report")) : rep.data && Object.keys(rep.data).length ? panel({ title: "This week" }, renderReport(rep.data, { labels: LABELS })) : emptyState("No report yet", "The report fills in after a few duos have been approved or rejected."),
      h("div", { class: "grid-2" },
        panel({ title: "Regression test", lead: "Runs a fixed set of 40 briefs through the plan and checks that nothing got worse." }, field("What to test", stage), field("How many", sample), regErr, run, h("p", { class: "muted small" }, `A part-picture test usually costs a few dollars per template. Tests above ${money(settings.budgets?.regression_ask_usd ?? 20, 0)} always ask first.`)),
        panel({ title: "New versions", lead: "A newer model version is only used by default after it passes the regression test and the variety guard." }, cands.length ? null : h("p", { class: "muted" }, "No candidate versions are waiting. Add one in Settings, under Models and versions."), field("Candidate", role), promoErr, promote)));
  };
  await draw();
}
