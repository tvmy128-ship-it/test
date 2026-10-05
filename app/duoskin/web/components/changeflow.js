// @ts-check
// After a "Change…" decision (APP_SPEC 9.6): the text is interpreted (L7), then either
//   * a CLARIFY gate asks one question, the answer goes back and L7 runs again, or
//   * the change is rejected (a hard rule would break; nothing is applied), or
//   * a CHANGE_CONFIRM gate shows the spec diff, the DNA-card diff, the parts to redo, the estimate and any soft warnings,
//     and the user confirms or cancels. Nothing runs before the confirm.
// `followChange` drives that loop with dialogs. The server is polled (GET /api/changes/{id} and the open gates of the
// project) every second, and gate.opened events make it react at once.
import { get, tryGet, friendly } from "../api.js";
import { h, money } from "../dom.js";
import { on as onEvent } from "../events.js";
import { openDialog } from "./modal.js";
import { diffList, invalidationList, pathList } from "./diff.js";
import { decide } from "./decisions.js";
import { toast } from "./toast.js";
import { warningText } from "./warnings.js";
import { spinner } from "./ui.js";

const POLL_MS = 1000;
const GIVE_UP_MS = 120000;

/** @param {number} ms @param {AbortSignal} [signal] */
const sleep = (ms, signal) => new Promise((r) => { const t = setTimeout(r, ms); signal?.addEventListener("abort", () => { clearTimeout(t); r(undefined); }); });

/**
 * @param {{projectId: string, changeId?: string | null, afterGateIds?: string[], onApplied?: () => void, partLabels?: Record<string, string>}} o
 * @returns {Promise<"applied" | "cancelled" | "rejected" | "timeout" | "closed">}
 */
export async function followChange(o) {
  const waiting = openDialog({ title: "Working out your change", dismissible: true,
    body: [h("p", { class: "row" }, spinner(), h("span", {}, "Reading what you asked for. Nothing is redone and nothing is charged until you confirm.")),
      h("p", { class: "muted" }, "This usually takes less than a minute.")] });
  let closed = false;
  waiting.closed.then(() => { closed = true; });
  const abort = new AbortController();
  const seen = new Set(o.afterGateIds ?? []);
  let wake = () => {};
  const off = onEvent("gate.opened", (e) => { if (e.project_id === o.projectId) wake(); });
  const started = Date.now();
  let changeId = o.changeId ?? null;
  try {
    while (!closed && Date.now() - started < GIVE_UP_MS) {
      /** @type {any} */ let cr = null;
      if (changeId) {
        const r = await tryGet(`/api/changes/${encodeURIComponent(changeId)}`).catch(() => ({ data: null, unavailable: true }));
        cr = r.data;
        if (cr?.status === "applied") { waiting.close(); toast("Change applied. The affected parts are being redone.", { kind: "ok" }); o.onApplied?.(); return "applied"; }
        if (cr?.status === "cancelled") { waiting.close(); return "cancelled"; }
        if (cr?.status === "rejected") { waiting.close(); await showRejected(cr); return "rejected"; }
      }
      const gates = await get("/api/gates", { query: { project_id: o.projectId, state: "open" } }).catch(() => []);
      const gate = /** @type {any[]} */ (gates).find((g) => ["clarify", "change_confirm"].includes(g.kind) && !seen.has(g.id));
      if (gate) {
        seen.add(gate.id);
        waiting.close();
        if (!changeId) changeId = gate.tiles?.[0]?.facts?.change_id ?? gate.tiles?.[0]?.facts?.change_request_id ?? null;
        const result = gate.kind === "clarify" ? await askClarify(gate) : await confirmChange(gate, changeId, o.partLabels ?? {});
        if (result === "cancelled") return "cancelled";
        if (result === "applied") { o.onApplied?.(); return "applied"; }
        // an answered question starts L7 again: keep waiting, in a fresh dialog
        return followChange({ ...o, changeId, afterGateIds: [...seen] });
      }
      await Promise.race([sleep(POLL_MS, abort.signal), new Promise((r) => { wake = () => r(undefined); })]);
    }
    if (!closed) { waiting.close(); toast("This is taking longer than expected. Check the Jobs page; your change is still being worked on.", { kind: "warn" }); return "timeout"; }
    return "closed";
  } finally {
    off();
    abort.abort();
  }
}

/** @param {any} cr */
function showRejected(cr) {
  const reason = cr?.plan?.reason || cr?.plan?.rejection || cr?.invalidation?.reason || "";
  /** @type {import("./modal.js").DialogHandle} */
  let dlg;
  dlg = openDialog({ title: "That change cannot be made", body: [
    h("p", {}, "It would break one of the rules that keep the design buildable on Roblox, so nothing was changed and nothing was charged."),
    reason ? h("p", { class: "note warn" }, String(reason)) : null,
    h("p", { class: "muted" }, "Try describing it a different way, for example a different colour or a smaller change.")],
  actions: h("button", { type: "button", class: "btn primary", onclick: () => dlg.close(true) }, "OK") });
  return dlg.closed;
}

/** @param {any} gate @returns {Promise<"answered" | "cancelled">} */
function askClarify(gate) {
  const tile = gate.tiles[0];
  const question = String(tile?.facts?.question || tile?.label || "Could you say a little more about what you want?");
  const answer = h("textarea", { id: "clarify-answer", rows: "3", maxlength: "1000" });
  const err = h("p", { class: "note bad", role: "alert" });
  /** @type {import("./modal.js").DialogHandle} */
  let dlg;
  const send = h("button", { type: "button", class: "btn primary", onclick: async () => {
    if (answer.value.trim().length < 2) { err.textContent = "Please type a short answer."; return; }
    send.disabled = true;
    const r = await decide(gate, tile, "change", { text: answer.value.trim() });
    if (r.status === "done") dlg.close("answered"); else { err.textContent = r.message ?? ""; send.disabled = false; }
  } }, "Send my answer");
  const cancel = h("button", { type: "button", class: "btn", onclick: async () => { await decide(gate, tile, "cancel"); dlg.close("cancelled"); } }, "Cancel the change");
  dlg = openDialog({ title: "A quick question", dismissible: false, body: [h("p", { class: "question" }, question), h("label", { for: "clarify-answer" }, "Your answer"), answer, err], actions: [cancel, send] });
  answer.focus();
  return dlg.closed.then((v) => (v === "answered" ? "answered" : "cancelled"));
}

/** @param {any} gate @param {string | null} changeId @param {Record<string, string>} labels @returns {Promise<"applied" | "cancelled">} */
async function confirmChange(gate, changeId, labels) {
  const tile = gate.tiles[0];
  const facts = tile?.facts ?? {};
  /** @type {any} */ let cr = null;
  if (changeId) { try { cr = await get(`/api/changes/${encodeURIComponent(changeId)}`); } catch (err) { toast(friendly(err), { kind: "warn" }); } }
  const diff = cr?.diff ?? facts.diff ?? {};
  const specChanges = diff.spec_changes ?? facts.spec_changes ?? [];
  const dnaChanges = diff.dna_changes ?? facts.dna_changes ?? [];
  const dnaPaths = facts.dna_diff ?? cr?.plan?.dna_diff ?? [];
  const estimate = cr?.estimate_usd ?? facts.estimate_usd;
  const warnings = /** @type {any[]} */ (facts.warnings ?? []);
  const body = [
    h("p", {}, "Here is what would change. Nothing has been redone yet."),
    h("section", {}, h("h3", {}, "What changes in the design"), diffList(specChanges.length ? specChanges : dnaChanges, { empty: "The design card itself does not change." }), dnaPaths.length ? pathList(dnaPaths) : null),
    h("section", {}, h("h3", {}, "Parts that will be redone"), invalidationList(cr?.invalidation ?? facts.invalidation ?? facts.redo ?? [], labels)),
    h("p", { class: "estimate" }, typeof estimate === "number" ? [h("strong", {}, `Estimated cost: ${money(estimate)}`), estimate === 0 ? " (nothing is charged: the program redoes this itself)" : " (an estimate; you will be asked again if a step would go over your cap)"] : "The cost estimate is not available yet."),
    warnings.length ? h("section", { class: "heads-up" }, h("p", { class: "heads-up-title" }, "Heads-up"), h("ul", {}, warnings.slice(0, 2).map((w) => h("li", {}, warningText(w))))) : null,
  ];
  /** @type {import("./modal.js").DialogHandle} */
  let dlg;
  const errNote = h("p", { class: "note bad", role: "alert" });
  const go = h("button", { type: "button", class: "btn primary", onclick: async () => {
    go.disabled = true;
    const r = await decide(gate, tile, "confirm");
    if (r.status === "done") dlg.close("applied"); else { errNote.textContent = r.message ?? ""; go.disabled = false; }
  } }, "Confirm the change");
  const no = h("button", { type: "button", class: "btn", onclick: async () => { await decide(gate, tile, "cancel"); dlg.close("cancelled"); } }, "Cancel, keep it as it is");
  dlg = openDialog({ title: "Confirm your change", wide: true, dismissible: false, body: [...body, errNote], actions: [no, go] });
  go.focus();
  const v = await dlg.closed;
  if (v === "applied") toast("Change confirmed. The affected parts are being redone.", { kind: "ok" });
  return v === "applied" ? "applied" : "cancelled";
}
