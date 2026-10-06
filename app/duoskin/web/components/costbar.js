// @ts-check
// The always-visible cost bar: what this duo has cost so far against its cap, plus today's total.
import { h, money, setChildren } from "../dom.js";

/**
 * @param {HTMLElement} host
 * @param {{demo?: boolean, todayUsd: number, project?: {name: string, spent_usd: number, settings: {budget_usd: number}} | null, queue?: any, doctor?: any, waiting?: number, running?: number}} d
 */
export function renderCostBar(host, d) {
  const kids = [];
  if (d.project) {
    const spent = d.project.spent_usd || 0;
    const cap = d.project.settings?.budget_usd || 0;
    const frac = cap > 0 ? spent / cap : 0;
    const tone = frac >= 1 ? "bad" : frac >= 0.8 ? "warn" : "ok";
    const meter = h("div", { class: `meter ${tone}`, role: "meter", "aria-valuemin": "0", "aria-valuemax": String(cap), "aria-valuenow": spent.toFixed(2),
      "aria-label": `Spent ${money(spent)} of the ${money(cap)} cap` }, h("div", { class: "meter-fill" }));
    /** @type {HTMLElement} */ (meter.firstElementChild).style.setProperty("--p", `${Math.min(100, Math.round(frac * 100))}%`);
    kids.push(h("div", { class: "cost-item" },
      h("span", { class: "cost-label", title: d.demo ? "Demo mode: these are pretend prices for the practice stand-ins. Nothing is charged." : "" }, d.demo ? "Pretend spend on this duo" : "Spent on this duo"),
      h("span", { class: "cost-value" }, money(spent), h("span", { class: "cost-cap" }, ` of ${money(cap)} cap`)),
      meter,
      frac >= 1 ? h("span", { class: "badge bad" }, "Cap reached") : frac >= 0.8 ? h("span", { class: "badge warn" }, "Close to the cap") : null));
  }
  kids.push(h("div", { class: "cost-item" }, h("span", { class: "cost-label", title: d.demo ? "Demo mode: these are pretend prices for the practice stand-ins. Nothing is charged." : "" }, d.demo ? "Pretend spend today" : "Today, all duos"), h("span", { class: "cost-value" }, money(d.todayUsd))));
  const q = d.queue;
  const pills = [];
  if (d.running) pills.push(h("a", { class: "pill busy", href: "#/jobs", title: "Steps that are working right now" }, `Working: ${d.running} ${d.running === 1 ? "step" : "steps"}`));
  if (q?.paused) pills.push(h("span", { class: "pill warn" }, "Queue paused"));
  if (q?.paid_blocked) pills.push(h("span", { class: "pill bad", title: String(q.paid_blocked) }, "Paid steps blocked"));
  if (d.waiting) pills.push(h("a", { class: "pill info", href: "#/" }, `${d.waiting} waiting on you`));
  if (d.doctor?.blocks_paid_features) pills.push(h("a", { class: "pill bad", href: "#/setup" }, "Setup needs attention"));
  else if (d.doctor?.summary?.warnings) pills.push(h("a", { class: "pill warn", href: "#/setup" }, "Setup warnings"));
  setChildren(host, h("div", { class: "costbar-inner", "aria-label": "Costs" }, kids, pills.length ? h("div", { class: "cost-pills" }, pills) : null));
}
