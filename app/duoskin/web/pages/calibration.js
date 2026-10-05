// @ts-check
// Calibration (#/calibration; APP_SPEC 3.8): quick blind rounds that teach the checks your taste. Locked until there are at
// least 5 approved duos. Drill sessions (clone / real duo / strangers, plus like or dislike, 10 to 15 minutes) and the judge
// calibration sets. The items are blind: no hints about what the answer is.
import { post, tryGet, friendly, ApiError, casImage } from "../api.js";
import { h, setChildren } from "../dom.js";
import { pageHeader, panel, tabList, notAvailable, errorNote, emptyState, progress } from "../components/ui.js";
import { renderReport } from "../components/report.js";
import { toast } from "../components/toast.js";

const NEEDED = 5;
const DRILL_LABELS = [["clone", "A copy of something"], ["real_duo", "A real duo"], ["strangers", "Two strangers, not a pair"]];
const JUDGE_LABELS = [["pass", "Looks right"], ["fail", "Something is wrong"]];

/** @param {import("../router.js").PageContext} ctx */
export async function render(ctx) {
  let kind = "drill";
  const content = h("div", {});
  const tabs = tabList([{ id: "drill", label: "Drill sessions" }, { id: "judge", label: "Judge calibration" }], kind, (id) => { kind = id; void draw(); }, "Calibration sections");
  setChildren(ctx.root, pageHeader({ title: "Calibration", lead: "Short blind rounds, 10 to 15 minutes. Your answers teach the checks what you would approve." }), tabs, content);
  let answered = 0;
  // a round is meant to take 10 to 15 minutes: a quiet timer, never a deadline
  const started = Date.now();
  const timer = h("p", { class: "muted small", "aria-live": "off" });
  const tick = () => { const sec = Math.floor((Date.now() - started) / 1000); timer.textContent = `Time so far: ${String(Math.floor(sec / 60)).padStart(2, "0")}:${String(sec % 60).padStart(2, "0")} (a round takes 10 to 15 minutes)`; };
  tick();
  const interval = setInterval(tick, 1000);
  ctx.onCleanup(() => clearInterval(interval));

  const draw = async () => {
    /** @type {{data: any, unavailable: boolean}} */ let res;
    try { res = await tryGet("/api/calibration/session", { query: { kind }, signal: ctx.signal }); } catch (err) {
      if (!ctx.active()) return;
      if (err instanceof ApiError && [403, 409, 423].includes(err.status)) { setChildren(content, locked(err.data?.approved ?? 0)); return; }
      setChildren(content, errorNote(friendly(err))); return;
    }
    if (!ctx.active()) return;
    if (res.unavailable) { setChildren(content, notAvailable("Calibration rounds")); return; }
    const d = res.data || {};
    if (d.locked) { setChildren(content, locked(d.approved_duos ?? d.approved ?? 0, d.needed ?? NEEDED)); return; }
    const items = /** @type {any[]} */ (d.items ?? []);
    const labels = kind === "drill" ? DRILL_LABELS : JUDGE_LABELS;
    const counts = d.counts ?? d.label_counts;
    const parts = [];
    if (!items.length) parts.push(emptyState("All done for now", "There is nothing left to look at. Come back after you approve more duos."));
    else {
      const item = items[0];
      const imgs = /** @type {string[]} */ (item.images ?? item.asset_shas ?? [item.image_sha ?? item.asset_sha].filter(Boolean));
      /** @param {string} label @param {boolean | undefined} like */
      const send = async (label, like) => {
        try {
          await post("/api/calibration/labels", { item_id: item.item_id ?? item.id, label, like });
          answered += 1;
          toast("Thanks, that helps.", { kind: "ok", ms: 1500 });
          await draw();
        } catch (e) { toast(friendly(e), { kind: "bad" }); }
      };
      parts.push(panel({ title: `Item ${answered + 1}` }, timer, progress(Math.min(1, answered / Math.max(items.length + answered, 1)), "Round progress"),
        h("div", { class: "strip" }, imgs.map((sha, i) => h("figure", { class: "fig" }, casImage(sha, { alt: `Picture ${i + 1} of this item` })))),
        item.question ? h("p", {}, String(item.question)) : h("p", {}, kind === "drill" ? "What is this?" : "Is anything wrong with this?"),
        h("div", { class: "row", role: "group", "aria-label": "Your answer" }, labels.map(([v, t]) => h("button", { type: "button", class: "btn", onclick: () => send(v, undefined) }, t))),
        kind === "drill" ? h("div", { class: "row", role: "group", "aria-label": "Do you like it?" }, h("span", { class: "muted" }, "And do you like it?"),
          h("button", { type: "button", class: "btn small", onclick: () => send("like", true) }, "Like"), h("button", { type: "button", class: "btn small", onclick: () => send("dislike", false) }, "Dislike")) : null,
        h("button", { type: "button", class: "btn quiet small", onclick: () => send("skip", undefined) }, "Skip this one")));
    }
    if (counts) parts.push(panel({ title: "How many answers there are" }, renderReport(counts), h("p", { class: "muted small" }, "Practice rounds are capped at a quarter of all answers so they never drown out your real choices.")));
    setChildren(content, ...parts);
  };
  await draw();
}

/** @param {number} approved @param {number} [needed] */
function locked(approved, needed = NEEDED) {
  return panel({}, h("h2", {}, "Not unlocked yet"), h("p", {}, `Calibration needs at least ${needed} approved duos so it has your real choices to compare with. You have ${approved}.`),
    progress(Math.min(1, approved / needed), "Approved duos"), h("a", { class: "btn primary", href: "#/new" }, "Make a duo"));
}
