// @ts-check
// Learning (#/learning; APP_SPEC 3.9, 12): the weekly look at what is working, the suggestions for the quality limits (never applied by
// themselves), the regression test with its variety guard, and the promotion of new versions. Every number comes from the API; the page only
// explains it. Nothing here costs money without a question first.
import { get, post, tryGet, friendly, ApiError } from "../api.js";
import { h, humanize, money, plural, setChildren, timeAgo } from "../dom.js";
import { pageHeader, panel, field, note, notAvailable, errorNote, emptyState, badge, progress } from "../components/ui.js";
import { confirmDialog } from "../components/modal.js";
import { toast } from "../components/toast.js";

const POLL_MS = 4000;
const pct = (/** @type {number | null | undefined} */ x) => `${Math.round((x ?? 0) * 100)}%`;
const num = (/** @type {number | null | undefined} */ x, d = 3) => (x === null || x === undefined ? "n/a" : Number(x).toFixed(d));
const PARTS_TEMPLATES = /** @type {[string, string][]} */ ([["I2", "Prints and patches (I2, R2)"], ["R1", "Faces (R1, I3)"], ["I4", "Hair (I4)"], ["I6", "Accessory badges (I6)"]]);

/** @param {import("../router.js").PageContext} ctx */
export async function render(ctx) {
  const body = h("div", { class: "page-body" });
  setChildren(ctx.root, pageHeader({ title: "Learning", lead: "A weekly look at what is working: which checks help, how much a duo costs, and how varied your duos are." }), body);
  let week = "";
  /** @type {any} */ let formState = { stage: "plan", sample: "10", template: "I2", candidate: "", compare: false };
  /** @type {any} */ let timer = null;
  ctx.onCleanup(() => clearTimeout(timer));

  let lastSig = "";
  /** @param {boolean} [auto] a refresh by the timer: it redraws only when a test run changed, so a form being filled in is left alone */
  const load = async (auto = false) => {
    clearTimeout(timer);
    /** @type {{data: any, unavailable: boolean}} */ let rep;
    try { rep = await tryGet("/api/learning/report", { query: { week: week || undefined }, signal: ctx.signal }); } catch (err) { if (ctx.active()) setChildren(body, errorNote(friendly(err))); return; }
    if (!ctx.active()) return;
    if (rep.unavailable) { setChildren(body, panel({ title: "This week" }, notAvailable("The weekly report"))); return; }
    const r = rep.data || {};
    if (!r.week) { setChildren(body, emptyState("No report yet", "The report fills in after a few duos have been approved or rejected.")); return; }
    const running = (r.regression?.runs ?? []).some((/** @type {any} */ x) => ["queued", "running"].includes(x.state));
    const sig = JSON.stringify((r.regression?.runs ?? []).map((/** @type {any} */ x) => [x.id, x.state, x.guard?.decision]));
    if (!(auto && sig === lastSig)) {
      lastSig = sig;
      const reload = () => load();
      setChildren(body, alerts(r, reload), stats(r), checksPanel(r), stylesPanel(r), causesPanel(r), tunerPanel(r, reload), regressionPanel(r, reload), versionsPanel(r, reload));
    }
    if (running) timer = setTimeout(() => { if (ctx.active()) void load(true); }, POLL_MS);
  };

  // ------------------------------------------------------------------------------------------------ notices
  /** @param {any} r @param {() => Promise<void>} reload */
  function alerts(r, reload) {
    const items = [];
    for (const a of r.alerts ?? []) items.push(note(String(a), "warn"));
    for (const d of r.demoted_checks ?? []) {
      const row = h("p", { class: "note warn", role: "status" }, h("span", {}, String(d.banner || `${d.title || humanize(d.check_id)} is set to warn only.`)),
        h("button", { type: "button", class: "btn small", onclick: async () => {
          try { await post("/api/learning/demotions/restore", { check_id: d.check_id }); toast("Turned back on.", { kind: "ok" }); await reload(); } catch (e) { toast(friendly(e), { kind: "bad" }); }
        } }, "Turn it back on"));
      items.push(row);
    }
    if ((r.hidden_warnings ?? []).length) {
      const names = r.hidden_warnings.map((/** @type {any} */ w) => w.title || humanize(w.check_id)).join("; ");
      items.push(note(`You set these notes aside most of the time, so they stay hidden until the limit behind them is re-tuned: ${names}.`, "info"));
    }
    for (const t of r.revisit_triggers ?? []) items.push(note(String(t.text), "info"));
    if (r.structure_hint) items.push(note(String(r.structure_hint), "info"));
    if (r.wildcard_hint) items.push(note(String(r.wildcard_hint), "info"));
    return items.length ? h("div", { class: "alert-list" }, items) : null;
  }

  // ------------------------------------------------------------------------------------------------ numbers
  /** @param {any} r */
  function stats(r) {
    const hard = r.hard_reject ?? {};
    const lab = r.labels ?? {};
    const cost = r.cost ?? {};
    const card = (/** @type {string} */ label, /** @type {string} */ value, /** @type {string} */ sub = "") => h("div", { class: "stat-card" }, h("span", { class: "stat-label" }, label), h("span", { class: "stat-value" }, value), sub ? h("span", { class: "stat-sub" }, sub) : null);
    return h("div", { class: "stat-grid" },
      card("Approved duos", String(r.approved_duos ?? 0), `${plural(Number(r.decisions?.approved ?? 0), "choice")} this ${week === "all" ? "time" : "week"}`),
      card("Cost per approved duo", money(r.cost_per_duo ?? cost.per_approved_duo ?? 0), cost.all_spend_per_approved_duo ? `${money(cost.all_spend_per_approved_duo)} with every abandoned try` : ""),
      card("Concepts approved first try", r.gate1?.duos ? pct(r.gate1_first_try_approval) : "n/a", r.gate1?.duos ? `of ${plural(r.gate1.duos, "duo")}` : "no duos yet"),
      card("Wildcard picked", r.wildcard?.duos ? pct(r.wildcard_pick_rate) : "n/a", r.wildcard?.duos ? `in your last ${plural(r.wildcard.duos, "duo")}` : "no duos yet"),
      card("Required checks stopped", pct(r.hard_reject_rate_on_approved), `of approved duos; the limit is ${pct(hard.max ?? 0.1)}`),
      card("Too like an earlier design", pct(r.registry_reject_rate), "faces and prints turned back for matching an old one"),
      card("Answers so far", `${Math.round(Number(lab.effective_total ?? 0))}`, `about ${lab.target ?? 200} are needed to tune the limits`),
      card("Practice share", pct(lab.drill_share), `capped at ${pct(lab.cap ?? 0.25)} of all answers`));
  }

  // ------------------------------------------------------------------------------------------------ the checks
  /** @param {any} r */
  function checksPanel(r) {
    const rows = /** @type {any[]} */ (r.per_check ?? []);
    const weekSel = h("select", { "aria-label": "Which period" }, [["", `This week (${r.week?.label ?? ""})`], ["all", "All time"]].map(([v, t]) => h("option", { value: v, selected: week === v }, t)));
    weekSel.addEventListener("change", () => { week = weekSel.value; void load(); });
    const rate = (/** @type {number} */ x, /** @type {number} */ n) => h("span", { class: "rate-cell" }, pct(x), " ", h("small", {}, `(${n})`));
    return panel({ title: "Which checks help", lead: "For every check: how often it warned about a duo you went on to approve, and how often it warned about one you turned down. A good check catches the ones you turn down and leaves the ones you approve alone.",
      actions: h("div", { class: "panel-tools" }, weekSel) },
      rows.length ? h("div", { class: "table-wrap" }, h("table", { class: "table compact" },
        h("thead", {}, h("tr", {}, ["Check", "Warned on duos you approved", "Warned on duos you turned down", "Shown", "You overrode", "State"].map((t) => h("th", {}, t)))),
        h("tbody", {}, rows.map((c) => h("tr", {},
          h("td", {}, c.title || humanize(c.check_id)),
          h("td", {}, rate(c.flag_rate_on_approved ?? 0, c.flagged_approved ?? 0)),
          h("td", {}, rate(c.catch_rate_on_rejected ?? 0, c.flagged_rejected ?? 0)),
          h("td", { class: "rate-cell" }, String(c.shown ?? 0)),
          h("td", { class: "rate-cell" }, String(c.overridden ?? 0)),
          h("td", {}, c.hidden ? badge("Hidden: you override it often", "warn") : c.demoted ? badge("Only warns now", "warn") : c.kind === "soft" ? badge("Warning", "muted") : badge("Required", "info"))))))) :
        emptyState("Nothing to show yet", "Each check appears here once it has flagged something you decided on."));
  }

  /** @param {any} r */
  function stylesPanel(r) {
    const use = Object.entries(r.structure_use ?? {});
    const dist = /** @type {any[]} */ (r.nearest_duo_distances?.series ?? []).slice(-10);
    const bars = (/** @type {[string, number][]} */ entries, /** @type {(v: number) => string} */ fmt) => h("ul", { class: "bars" }, entries.map(([label, v]) => {
      const fill = h("span", { class: "bar-fill" });
      fill.style.setProperty("--p", `${Math.round(Math.min(1, Number(v)) * 100)}%`);
      return h("li", {}, h("span", {}, label), h("span", { class: "bar-track" }, fill), h("span", { class: "bar-value" }, fmt(Number(v))));
    }));
    return h("div", { class: "grid-2" },
      panel({ title: "How often each pair style was used", lead: "A pair style that is more than a third of your duos may be crowding out the others." },
        use.length ? bars(/** @type {[string, number][]} */ (use.map(([k, v]) => [humanize(k), Number(v)])), pct) : h("p", { class: "muted" }, "No approved duos yet.")),
      panel({ title: "How different each duo is from the closest earlier one", lead: "Lower bars mean a duo looks more like one you already made." },
        dist.length ? [bars(dist.map((s, i) => [`Duo ${i + 1} of the last ${dist.length}`, Number(s.distance)]), (v) => num(v, 2)),
          r.nearest_duo_distances?.shrinking ? note("Your newest duos are getting closer to your earlier ones.", "warn") : null] : h("p", { class: "muted" }, "This needs at least two approved duos.")));
  }

  /** @param {any} r */
  function causesPanel(r) {
    const rows = /** @type {any[]} */ (r.top_failure_causes ?? []);
    if (!rows.length) return null;
    // plain words on screen; the internal ids only live in the hover text, for support
    return panel({ title: "What went wrong most often", lead: "The checks that failed most often, in plain words." },
      h("div", { class: "table-wrap" }, h("table", { class: "table compact" }, h("thead", {}, h("tr", {}, ["What failed", "Times"].map((t) => h("th", {}, t)))),
        h("tbody", {}, rows.map((c) => h("tr", { title: `${c.fm_id}: ${(c.check_ids ?? []).join(", ")}` }, h("td", {}, String(c.label || (c.titles ?? []).join("; ") || "A check")), h("td", { class: "rate-cell" }, String(c.failures))))))));
  }

  // ------------------------------------------------------------------------------------------------ the quality limits
  /** @param {any} r @param {() => Promise<void>} reload */
  function tunerPanel(r, reload) {
    const t = r.tuner ?? {};
    const result = h("div", { class: "msg" });
    const list = h("div", {});
    /** @type {Set<string>} */ const chosen = new Set();
    const accept = h("button", { type: "button", class: "btn primary", disabled: true }, "Test and accept the ticked limits");
    const draw = (/** @type {any} */ rep) => {
      const order = { proposed: 0, unchanged: 1, insufficient: 2, not_ready: 3 };
      const props = /** @type {any[]} */ ([...(rep?.proposals ?? [])].sort((a, b) => (order[/** @type {keyof typeof order} */ (a.status)] ?? 4) - (order[/** @type {keyof typeof order} */ (b.status)] ?? 4)));
      chosen.clear();
      accept.disabled = true;
      if (!props.length) { setChildren(list); return; }
      setChildren(list, h("div", { class: "table-wrap" }, h("table", { class: "table compact" },
        h("thead", {}, h("tr", {}, ["", "What it limits", "Now", "Suggested", "Based on", ""].map((x) => h("th", {}, x)))),
        h("tbody", {}, props.map((p) => {
          const box = h("input", { type: "checkbox", "aria-label": `Accept the limit for ${p.what}`, disabled: p.status !== "proposed" });
          box.addEventListener("change", () => { if (box.checked) chosen.add(p.key); else chosen.delete(p.key); accept.disabled = chosen.size === 0; });
          return h("tr", {}, h("td", {}, box), h("td", {}, p.what || humanize(p.key)), h("td", { class: "rate-cell" }, String(p.current)),
            h("td", { class: "rate-cell" }, p.proposed === null || p.proposed === undefined ? "none" : String(p.proposed)),
            h("td", {}, p.status === "insufficient" || p.status === "not_ready" ? p.note : `${plural(p.n_approved + p.n_drill, "duo")}${p.note ? `; ${p.note}` : ""}`),
            h("td", {}, p.status === "proposed" ? badge("Suggestion", "info") : p.status === "unchanged" ? badge("Already right", "ok") : badge("Waiting", "muted")));
        })))));
    };
    draw(rep0(r));
    const run = h("button", { type: "button", class: "btn", onclick: async () => {
      setChildren(result);
      try { const rep = await post("/api/learning/tuner/run", {}); draw(rep); if (rep.note) setChildren(result, note(String(rep.note), "info")); } catch (e) { setChildren(result, errorNote(friendly(e))); }
    } }, "Look for suggestions");
    accept.addEventListener("click", async () => {
      setChildren(result);
      if (!(await confirmDialog({ title: "Test these limits?", message: "DuoSkin lints your last regression plans again with the new limits. They are only used if variety and quality do not fall.", confirmLabel: "Test and accept" }))) return;
      try { await post("/api/learning/thresholds/accept", { keys: [...chosen] }); toast("The new limits are in use.", { kind: "ok" }); await reload(); } catch (e) {
        const reasons = e instanceof ApiError && Array.isArray(e.data?.guard?.reasons) ? e.data.guard.reasons.join(" ") : "";
        setChildren(result, e instanceof ApiError && e.status === 409 ? note(`Not accepted. ${reasons || friendly(e)}`, "warn") : errorNote(friendly(e)));
      }
    });
    const active = Object.entries(t.active ?? {});
    return panel({ title: "Quality limits", lead: "DuoSkin only ever suggests a limit that would have flagged about 1 duo in 20 of the ones you approved. It never aims for a look, and it never changes a limit by itself." },
      h("div", { class: "tuner-bar" }, progress(Math.min(1, Number(t.labels ?? 0) / Math.max(1, Number(t.target ?? 200))), "Answers collected"),
        h("p", { class: "muted small" }, t.ready ? "There are enough answers to make suggestions." : `${Math.round(Number(t.labels ?? 0))} of about ${t.target ?? 200} answers so far. Suggestions start when there are enough.`)),
      h("div", { class: "row" }, run, accept), result, list,
      active.length ? note(`Limits you accepted: ${active.map(([k, v]) => `${humanize(k)} ${v}`).join("; ")}.`, "ok") : null);
  }
  /** The last stored tuner run, if the report carries one. @param {any} r */
  function rep0(r) { const o = r.tuner?.open_proposals; return Array.isArray(o) && o.length ? { proposals: o } : null; }

  // ------------------------------------------------------------------------------------------------ the regression test
  /** @param {any} r @param {() => Promise<void>} reload */
  function regressionPanel(r, reload) {
    const reg = r.regression ?? {};
    const cands = /** @type {any[]} */ ((reg.versions?.roles ?? []).filter((/** @type {any} */ x) => x.candidate));
    const fs = formState;
    const stage = h("select", { "aria-label": "Which part to test" }, [["plan", "Plans and concept pictures"], ["parts", "Part pictures (one template at a time)"]].map(([v, t]) => h("option", { value: v, selected: fs.stage === v }, t)));
    const template = h("select", { "aria-label": "Which part template" }, PARTS_TEMPLATES.map(([v, t]) => h("option", { value: v, selected: fs.template === v }, t)));
    const sample = h("select", { "aria-label": "How many" }, [[String(reg.sample ?? 10), `A sample of ${reg.sample ?? 10} (cheaper)`], [String(reg.briefs ?? 40), `All ${reg.briefs ?? 40}`]].map(([v, t]) => h("option", { value: v, selected: fs.sample === v }, t)));
    const cand = h("select", { "aria-label": "Which version to test" }, [h("option", { value: "" }, "None: measure the versions in use now"),
      ...cands.map((c) => h("option", { value: `${c.role}|${c.candidate}`, selected: fs.candidate === `${c.role}|${c.candidate}` }, `${humanize(c.role)}: ${c.candidate}`))]);
    const compare = h("input", { type: "checkbox", id: "reg-compare", checked: !!fs.compare });
    const estimate = h("p", { class: "estimate-line", "aria-live": "polite" });
    const err = h("div", { class: "msg" });
    const sync = () => { Object.assign(fs, { stage: stage.value, sample: sample.value, template: template.value, candidate: cand.value, compare: compare.checked }); };
    const body = () => {
      sync();
      const [role, version] = cand.value ? cand.value.split("|") : ["", ""];
      return { stage: fs.stage, sample: Number(fs.sample), templates: fs.stage === "parts" ? [fs.template] : [], candidate_versions: role ? { [role]: version } : {}, compare: !role && fs.compare };
    };
    const refresh = async () => {
      sync();
      template.closest(".field")?.toggleAttribute("hidden", fs.stage !== "parts");
      try { const e = await get("/api/regression/estimate", { query: { stage: fs.stage, sample: Number(fs.sample) }, signal: ctx.signal }); setChildren(estimate, `${e.text}${e.needs_confirmation ? " DuoSkin asks again before anything is charged." : ""}`); } catch { setChildren(estimate); }
    };
    for (const el of [stage, template, sample, cand]) el.addEventListener("change", () => void refresh());
    compare.addEventListener("change", sync);
    const start = h("button", { type: "button", class: "btn primary", onclick: async () => {
      setChildren(err);
      sync();
      const b = body();
      const ok = await confirmDialog({ title: "Run the regression test?", message: h("div", {}, h("p", {}, "It runs the same fixed briefs through the plan and checks that nothing got worse. Nothing is approved, exported or remembered."),
        h("p", { class: "estimate-line" }, estimate.textContent || "")), confirmLabel: "Start the test" });
      if (!ok) return;
      try {
        await post("/api/regression/run", b);
        toast("Started. You can watch it on the Jobs page.", { kind: "ok" });
        await reload();
      } catch (e) { setChildren(err, e instanceof ApiError && e.unavailable ? note("The regression test is not available yet in this build.", "warn") : note(friendly(e), "warn")); }
    } }, "Start the regression test");

    const runs = /** @type {any[]} */ (reg.runs ?? []);
    const box = panel({ title: "Regression test", lead: `Runs ${reg.briefs ?? 40} fixed briefs through the plan (or a few frozen duos through one part template) and compares the result with the last run. A change is only kept if variety does not fall by more than 5% and quality does not fall at all.` },
      h("div", { class: "form-grid" }, field("What to test", stage), field("Which template", template), field("How many", sample), field("Version to test", cand)),
      h("div", { class: "row" }, compare, h("label", { for: "reg-compare" }, "I already changed something (a template, the house style, the kits): compare with the last run")),
      estimate, err, h("div", { class: "row" }, start),
      runs.length ? h("ul", { class: "run-list" }, runs.map((x) => runItem(x, reload))) : h("p", { class: "muted" }, "No test has been run yet. Run it once before you change anything: that run is what later versions are compared with."));
    void refresh();
    return box;
  }

  /** @param {any} x @param {() => Promise<void>} reload */
  function runItem(x, reload) {
    const g = x.guard;
    const verdict = x.state !== "done" ? badge(x.state === "stopped" ? "Stopped" : "Running", x.state === "stopped" ? "muted" : "busy")
      : !g ? badge(x.role === "baseline" ? "Baseline" : "Measured", "info") : g.accepted ? badge("Passed the guard", "ok") : g.decision === "reject" ? badge("Did not pass", "bad") : badge(humanize(g.decision), "warn");
    const what = `${x.stage === "plan" ? "Plans" : "Part pictures"}: ${x.role === "baseline" ? "versions in use" : Object.entries(x.candidate_versions?.models ?? {}).map(([k, v]) => `${humanize(k)} ${v}`).join(", ") || x.label || "a change"}`;
    const cell = (/** @type {string} */ label, /** @type {string} */ value) => h("span", { class: "run-num" }, h("small", {}, label), value);
    const checks = /** @type {any[]} */ (g?.checks ?? []);
    return h("li", {}, h("details", { class: "run" },
      h("summary", {}, h("span", { class: "run-what" }, what, h("small", { class: "muted" }, ` ${timeAgo(x.created_at)}`)),
        cell("Briefs made", `${x.ok_briefs ?? 0} of ${x.n_briefs ?? 0}`), cell("Quality", x.state === "done" ? num(x.quality) : "n/a"), cell("Picture variety", x.state === "done" ? num(x.image_distance) : "n/a"),
        cell("Variety index", x.state === "done" ? num(x.variety_index) : "n/a"), verdict),
      h("div", { class: "run-body" },
        x.mode === "mock" ? note("This run used practice stand-ins, so nothing was charged and the numbers only test the machinery.", "info") : null,
        x.brief_set_edited ? note("You edited the brief set; its fingerprint is part of this result.", "info") : null,
        g ? h("ul", { class: "guard-reasons" }, (g.reasons ?? []).map((/** @type {string} */ t) => h("li", {}, t))) : null,
        checks.length ? h("ul", { class: "guard-checks" }, checks.filter((c) => c.gating).map((c) => h("li", {}, c.ok ? badge("Fine", "ok") : badge("Failed", "bad"), h("span", {}, c.label),
          h("span", { class: "rate-cell muted" }, c.name.startsWith("served:") ? (c.ok ? "used" : "not used") : `${num(c.baseline)} to ${num(c.candidate)} (${(Number(c.change) * 100).toFixed(1)}%)`)))) : null,
        x.cost_usd ? h("p", { class: "muted small" }, `Cost: ${money(x.cost_usd)}`) : null,
        x.role === "candidate" && g?.accepted && !x.candidate_versions?.models && x.state === "done" ? h("button", { type: "button", class: "btn small", onclick: async () => {
          try { await post(`/api/regression/runs/${x.id}/adopt`); toast("This run is the new baseline.", { kind: "ok" }); await reload(); } catch (e) { toast(friendly(e), { kind: "bad" }); }
        } }, "Use this run as the new baseline") : null)));
  }

  // ------------------------------------------------------------------------------------------------ new versions
  /** @param {any} r @param {() => Promise<void>} reload */
  function versionsPanel(r, reload) {
    const roles = /** @type {any[]} */ ((r.regression?.versions?.roles ?? []).filter((/** @type {any} */ x) => x.candidate));
    const err = h("div", { class: "msg" });
    return panel({ title: "New versions", lead: "A newer model version is only used by default after it passed the regression test and the variety guard." },
      roles.length ? h("div", { class: "table-wrap" }, h("table", { class: "table compact version-table" },
        h("thead", {}, h("tr", {}, ["What", "In use now", "Waiting", "Test result", ""].map((t) => h("th", {}, t)))),
        h("tbody", {}, roles.map((v) => {
          const g = v.run?.guard;
          const result = !v.run ? badge("Not tested yet", "muted") : v.run.state !== "done" ? badge("Running", "busy") : g?.accepted ? badge("Passed", "ok") : badge("Did not pass", "bad");
          const promote = h("button", { type: "button", class: "btn small primary", disabled: !v.can_promote, onclick: async () => {
            setChildren(err);
            try { await post("/api/versions/promote", { role: v.role, version: v.candidate }); toast("Promoted. New duos will use it.", { kind: "ok" }); await reload(); } catch (e) {
              setChildren(err, note(e instanceof ApiError && e.status >= 400 && e.status < 500 ? `Not yet: ${friendly(e)} A version is only promoted after the latest regression test passed.` : friendly(e), "warn"));
            }
          } }, "Promote this version");
          const test = h("button", { type: "button", class: "btn small", onclick: async () => {
            setChildren(err);
            if (!(await confirmDialog({ title: `Test ${humanize(v.role)} ${v.candidate}?`, message: "This runs the regression test with this version switched on. It asks again before anything is charged.", confirmLabel: "Start the test" }))) return;
            try { await post("/api/regression/run", { stage: "plan", sample: Number(r.regression?.sample ?? 10), candidate_versions: { [v.role]: v.candidate } }); toast("Started. You can watch it on the Jobs page.", { kind: "ok" }); await reload(); } catch (e) {
              setChildren(err, note(friendly(e), "warn"));
            }
          } }, "Test it");
          return h("tr", {}, h("td", {}, humanize(v.role)), h("td", {}, String(v.default)), h("td", {}, String(v.candidate)), h("td", {}, result), h("td", {}, h("div", { class: "row" }, test, promote)));
        })))) :
        h("p", { class: "muted" }, "No candidate versions are waiting. Add one in Settings, under Models and versions."),
      err);
  }

  await load();
}
