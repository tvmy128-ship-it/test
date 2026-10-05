// @ts-check
// Jobs (#/jobs): the queue. Steps by state and provider, a rough ETA, pause/resume per project, retry a failed step,
// cancel, and the error with its plain-English hint.
import { get, post, friendly } from "../api.js";
import { h, humanize, timeAgo, plural, setChildren } from "../dom.js";
import { pageHeader, panel, badge, progress, emptyState, errorNote } from "../components/ui.js";
import { toast } from "../components/toast.js";
import { confirmDialog } from "../components/modal.js";
import { jobKindLabel, jobStateLabel, stepLabel, stepStateLabel, providerShort } from "../text.js";
import { state as store } from "../store.js";

const ACTIVE = ["running", "waiting_user", "paused"];

/** @param {string} s */
const tone = (s) => (s === "succeeded" ? "ok" : s === "failed" ? "bad" : s === "cancelled" ? "muted" : s === "waiting_user" ? "info" : s === "paused" ? "warn" : "busy");

/** @param {any[]} steps recent finished steps */
function averageSeconds(steps) {
  const times = steps.filter((s) => s.finished_at && s.created_at).map((s) => (Date.parse(s.finished_at) - Date.parse(s.created_at)) / 1000).filter((t) => t > 0 && t < 3600);
  return times.length ? times.reduce((a, b) => a + b, 0) / times.length : 0;
}

/** @param {import("../router.js").PageContext} ctx */
export async function render(ctx) {
  const body = h("div", {});
  const filter = h("select", { "aria-label": "Show jobs" }, [["active", "Running and waiting"], ["all", "All recent jobs"], ["failed", "Only failed"]].map(([v, t]) => h("option", { value: v }, t)));
  setChildren(ctx.root, pageHeader({ title: "Jobs", lead: "Everything DuoSkin is working on or waiting for, in one place." }), h("div", { class: "row" }, h("label", {}, "Show "), filter), body);
  const open = new Set();
  const detail = new Map();

  const draw = async () => {
    /** @type {any[]} */ let jobs; /** @type {any} */ let queue;
    try {
      [jobs, queue] = await Promise.all([get("/api/jobs", { query: { steps: "true", limit: 100 }, signal: ctx.signal }), get("/api/queue", { signal: ctx.signal })]);
    } catch (err) { if (ctx.active()) setChildren(body, errorNote(friendly(err))); return; }
    if (!ctx.active()) return;
    const names = new Map(/** @type {any[]} */ (store.snapshot?.projects ?? []).map((p) => [p.id, p.name]));
    const allSteps = /** @type {any[]} */ (jobs.flatMap((j) => j.steps || []));
    const avgByKind = new Map();
    for (const kind of new Set(allSteps.map((s) => s.kind))) avgByKind.set(kind, averageSeconds(allSteps.filter((s) => s.kind === kind && s.state === "succeeded" && !s.cached)));
    const wanted = filter.value;
    const list = jobs.filter((j) => (wanted === "all" ? true : wanted === "failed" ? j.job.state === "failed" || j.steps_by_state?.failed : ACTIVE.includes(j.job.state)));
    const parts = [];
    if (queue.paused || Object.keys(queue.paused_providers || {}).length || queue.paid_blocked) {
      parts.push(panel({ class: "gate-panel" }, h("h2", {}, "The queue is paused"), h("p", {}, queue.paid_blocked ? `Paid steps are blocked: ${queue.paid_blocked}` : Object.keys(queue.paused_providers).length ? `Paused for: ${Object.keys(queue.paused_providers).map(providerShort).join(", ")}. This usually means a key or credit problem.` : "Nothing new will start until you resume."),
        h("button", { type: "button", class: "btn primary", onclick: async () => { try { await post("/api/queue/resume"); toast("Resumed.", { kind: "ok" }); await draw(); } catch (e) { toast(friendly(e), { kind: "bad" }); } } }, "Resume the queue")));
    }
    // per-provider summary
    const active = allSteps.filter((s) => ["running", "waiting_remote", "ready", "pending"].includes(s.state));
    /** @type {Map<string, {n: number, secs: number}>} */
    const byProvider = new Map();
    for (const s of active) {
      const prov = s.pool === "api" ? (/^plan\./.test(s.kind) || /^duo\.(judge|ip|sim|second)/.test(s.kind) || /^img\.gate_b|^mesh\.judge/.test(s.kind) ? "anthropic" : /^tripo|^mv\./.test(s.kind) ? "tripo" : /^face|^img\.|^concept|^library/.test(s.kind) ? "openai" : "other") : "this computer";
      const e = byProvider.get(prov) ?? { n: 0, secs: 0 };
      e.n += 1; e.secs += avgByKind.get(s.kind) || 0;
      byProvider.set(prov, e);
    }
    if (byProvider.size) {
      parts.push(panel({ title: "Waiting on" }, h("ul", { class: "provider-eta" }, [...byProvider.entries()].map(([p, e]) => h("li", {}, h("strong", {}, p === "this computer" || p === "other" ? humanize(p) : providerShort(p)), ` ${plural(e.n, "step")}`, e.secs ? h("span", { class: "muted" }, ` · about ${Math.max(1, Math.round(e.secs / 60))} min`) : null)))));
    }
    if (!list.length) parts.push(emptyState("Nothing here", wanted === "active" ? "No jobs are running or waiting. Start a duo and its jobs appear here." : "No jobs match."));
    else {
      parts.push(h("div", { class: "table-wrap" }, h("table", { class: "table jobs-table" },
        h("thead", {}, h("tr", {}, ["Job", "Duo", "State", "Steps", ""].map((t) => h("th", {}, t)))),
        h("tbody", {}, list.flatMap((j) => {
          const counts = Object.entries(j.steps_by_state || {}).map(([k, v]) => `${v} ${stepStateLabel(k).toLowerCase()}`).join(", ");
          const failed = j.steps_by_state?.failed || 0;
          const pid = j.job.project_id;
          const row = h("tr", { dataset: { jobId: j.job.id } },
            h("td", {}, h("strong", {}, jobKindLabel(j.job.kind)), h("br"), h("span", { class: "muted small" }, timeAgo(j.job.created_at))),
            h("td", {}, pid ? h("a", { href: `#/p/${pid}` }, names.get(pid) ?? "A duo") : h("span", { class: "muted" }, "General")),
            h("td", {}, badge(jobStateLabel(j.job.state), /** @type {any} */ (tone(j.job.state)))),
            h("td", {}, counts || "—"),
            h("td", {}, h("div", { class: "row" },
              h("button", { type: "button", class: "btn small", "aria-expanded": String(open.has(j.job.id)), onclick: async () => {
                if (open.has(j.job.id)) open.delete(j.job.id); else { open.add(j.job.id); try { detail.set(j.job.id, await get(`/api/jobs/${j.job.id}`)); } catch (e) { toast(friendly(e), { kind: "bad" }); } }
                await draw();
              } }, open.has(j.job.id) ? "Hide steps" : failed ? `Show ${failed} failed` : "Show steps"),
              pid && ACTIVE.includes(j.job.state) ? h("button", { type: "button", class: "btn small", onclick: async () => {
                const p = /** @type {any[]} */ (store.snapshot?.projects ?? []).find((x) => x.id === pid);
                try { await post(`/api/projects/${pid}/${p?.paused ? "resume" : "pause"}`); await draw(); } catch (e) { toast(friendly(e), { kind: "bad" }); }
              } }, (store.snapshot?.projects ?? []).find((x) => x.id === pid)?.paused ? "Resume duo" : "Pause duo") : null,
              ACTIVE.includes(j.job.state) ? h("button", { type: "button", class: "btn small danger", onclick: async () => {
                if (!(await confirmDialog({ title: "Cancel this job?", message: "Running steps are stopped. Finished work is kept.", confirmLabel: "Cancel the job", cancelLabel: "Keep going", tone: "danger" }))) return;
                try { await post(`/api/jobs/${j.job.id}/cancel`); toast("Cancelled.", { kind: "ok" }); await draw(); } catch (e) { toast(friendly(e), { kind: "bad" }); }
              } }, "Cancel") : null)));
          const out = [row];
          if (open.has(j.job.id)) {
            const steps = /** @type {any[]} */ (detail.get(j.job.id)?.steps ?? j.steps ?? []);
            out.push(h("tr", { class: "detail-row" }, h("td", { colspan: "5" }, steps.length ? h("ol", { class: "timeline compact" }, steps.filter((s) => s.state !== "superseded").map((s) => h("li", { class: `step ${s.state}` },
              h("span", { class: "step-main" }, h("strong", {}, stepLabel(s.kind)), s.paid ? h("span", { class: "muted" }, " · paid") : null, s.part_id ? h("span", { class: "muted" }, ` · ${s.part_id}`) : null),
              badge(stepStateLabel(s.state), /** @type {any} */ (tone(s.state === "waiting_remote" ? "running" : s.state))),
              ["running", "waiting_remote"].includes(s.state) ? progress(s.progress || 0, stepLabel(s.kind)) : null,
              s.message && s.state !== "failed" ? h("span", { class: "step-msg muted" }, s.message) : null,
              s.state === "failed" ? h("span", { class: "step-error" }, s.error?.user_hint || "This step did not work.", " ", h("button", { type: "button", class: "btn small", onclick: async () => { try { await post(`/api/steps/${s.id}/retry`); toast("Trying again.", { kind: "ok" }); detail.set(j.job.id, await get(`/api/jobs/${j.job.id}`)); await draw(); } catch (e) { toast(friendly(e), { kind: "bad" }); } } }, "Try again")) : null))) : h("p", { class: "muted" }, "No steps yet."))));
          }
          return out;
        })))));
    }
    setChildren(body, ...parts);
  };
  filter.addEventListener("change", () => { void draw(); });
  await draw();
  ctx.live(["step.state", "job.state", "gate.opened", "gate.updated", "project.stage"], async () => {
    for (const jid of open) { try { detail.set(jid, await get(`/api/jobs/${jid}`)); } catch { /* the job may be gone */ } }
    void draw();
  }, 500);
}
