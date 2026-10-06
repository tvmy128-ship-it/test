// @ts-check
// Costs (#/costs): the ledger. Totals per provider and per duo, estimate vs actual, orphan rows, the Tripo balance and the
// date of the price table.
import { get, friendly } from "../api.js";
import { h, money, humanize, fmtDateTime, setChildren } from "../dom.js";
import { pageHeader, panel, badge, emptyState, errorNote, note } from "../components/ui.js";
import { providerLabel, providerShort } from "../text.js";
import { state as store } from "../store.js";

const BASIS = /** @type {Record<string, [string, string]>} */ ({
  estimate: ["Estimate", "info"], usage: ["Actual", "ok"], credits: ["Actual (credits)", "ok"], orphan: ["Possibly charged", "warn"],
});

/** @param {import("../router.js").PageContext} ctx */
export async function render(ctx) {
  const body = h("div", { class: "page-body" });
  const projects = /** @type {any[]} */ (store.snapshot?.projects ?? []);
  const sel = h("select", { "aria-label": "Show costs for" }, h("option", { value: "" }, "All duos"), projects.map((p) => h("option", { value: p.id, selected: p.id === ctx.query.get("project") }, p.name)));
  setChildren(ctx.root, pageHeader({ title: "Costs", lead: "Every call that costs money, what we expected, and what it really cost." }), h("div", { class: "row" }, h("label", {}, "Show "), sel), body);

  const draw = async () => {
    /** @type {any} */ let data;
    try { data = await get("/api/costs", { query: { project_id: sel.value || undefined }, signal: ctx.signal }); } catch (err) { if (ctx.active()) setChildren(body, errorNote(friendly(err))); return; }
    if (!ctx.active()) return;
    const t = data.totals || {};
    const rows = /** @type {any[]} */ (data.rows || []);
    const names = new Map(projects.map((p) => [p.id, p.name]));
    const byProvider = Object.entries(t.by_provider || {}).sort((a, b) => Number(b[1]) - Number(a[1]));
    const maxP = Math.max(0.01, ...byProvider.map(([, v]) => Number(v)));
    /** @type {Map<string, number>} */
    const byProject = new Map();
    for (const r of rows) if (["committed", "orphan"].includes(r.state)) byProject.set(r.project_id ?? "", (byProject.get(r.project_id ?? "") ?? 0) + r.usd);
    const orphans = rows.filter((r) => r.state === "orphan" || r.basis === "orphan");
    const card = (/** @type {string} */ label, /** @type {string} */ value, /** @type {string} */ sub = "") => h("div", { class: "stat-card" }, h("span", { class: "stat-label" }, label), h("span", { class: "stat-value" }, value), sub ? h("span", { class: "muted small" }, sub) : null);
    const demo = Boolean(store.health?.demo);      // practice stand-ins have pretend prices: nothing was charged
    const summary = h("div", { class: "stat-grid" },
      card(demo ? "Pretend spend" : "Spent", money(t.spent_usd), demo ? "practice prices, nothing is charged" : "charged so far"),
      card("Today", money(t.today_usd), "all duos"),
      card("Set aside for steps running now", money(t.reserved_usd), "released if a step does not run"),
      card("Tripo credits left", data.tripo_available_credits == null ? "Not known yet" : String(Math.round(data.tripo_available_credits)), "for 3D models"));
    setChildren(body, summary,
      orphans.length ? note(`${orphans.length} call${orphans.length === 1 ? "" : "s"} may have been charged without a result coming back (${money(t.orphan_usd)} counted). They are included in the totals so the cap stays honest.`, "warn") : null,
      h("div", { class: "grid-2" },
        panel({ title: "By service" }, byProvider.length ? h("ul", { class: "bars" }, byProvider.map(([p, v]) => {
          const bar = h("span", { class: "bar-fill" });
          bar.style.setProperty("--p", `${Math.round((Number(v) / maxP) * 100)}%`);
          return h("li", {}, h("span", { class: "bar-label" }, providerLabel(p)), h("span", { class: "bar-track" }, bar), h("span", { class: "bar-value" }, money(Number(v))));
        })) : h("p", { class: "muted" }, "Nothing has been charged yet.")),
        panel({ title: "By duo" }, byProject.size ? h("ul", { class: "bars" }, [...byProject.entries()].sort((a, b) => b[1] - a[1]).map(([pid, v]) => h("li", {}, h("span", { class: "bar-label" }, pid ? (names.get(pid) ?? "A duo") : "General"), h("span", { class: "bar-value" }, money(v))))) : h("p", { class: "muted" }, "Nothing has been charged yet."))),
      panel({ title: "Every charge", lead: data.price_table ? `Prices as of ${data.price_table}.` : "" },
        rows.length ? h("div", { class: "table-wrap" }, h("table", { class: "table compact" },
          h("thead", {}, h("tr", {}, ["When", "Duo", "Service", "What", "Amount", "Kind"].map((c) => h("th", {}, c)))),
          h("tbody", {}, rows.slice(0, 200).map((r) => {
            const [label, tone] = BASIS[r.basis] ?? [humanize(r.basis), "muted"];
            return h("tr", { class: r.state === "released" ? "muted" : "" }, h("td", {}, fmtDateTime(r.ts)), h("td", {}, r.project_id ? (names.get(r.project_id) ?? "A duo") : "General"), h("td", {}, providerShort(r.provider)),
              h("td", {}, humanize(String(r.operation).split(":")[0].replace(/\./g, " "))), h("td", {}, money(r.usd, 3), r.credits ? h("span", { class: "muted" }, ` (${r.credits} credits)`) : null),
              h("td", {}, badge(r.state === "reserved" ? "Set aside" : r.state === "released" ? "Released" : label, r.state === "reserved" ? "busy" : r.state === "released" ? "muted" : /** @type {any} */ (tone))));
          })))) : emptyState("No charges yet", "When something that costs money runs, it is listed here.")));
  };
  sel.addEventListener("change", () => { void draw(); });
  await draw();
  ctx.live(["cost.added", "budget.low"], () => { void draw(); }, 700);
}
