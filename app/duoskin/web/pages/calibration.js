// @ts-check
// Calibration (#/calibration; APP_SPEC 3.8, 12): short BLIND rounds made from your own approved duos. Each picture asks one question (is it a
// copy, a real duo, or two strangers) and, if you like, whether you like it. Nothing on this page says what the checks think: a hint would
// teach you to agree with the machine. Locked until there are 5 approved duos; practice answers are capped at a quarter of all answers.
import { post, tryGet, friendly, ApiError, casImage } from "../api.js";
import { h, plural, setChildren } from "../dom.js";
import { pageHeader, panel, tabList, notAvailable, errorNote, emptyState, progress, note } from "../components/ui.js";
import { toast } from "../components/toast.js";

const NEEDED = 5;
const DRILL_CHOICES = /** @type {[string, string][]} */ ([["clone", "A copy of something"], ["real_duo", "A real duo"], ["strangers", "Two strangers, not a pair"]]);
const JUDGE_CHOICES = /** @type {[string, string][]} */ ([["pass", "Looks right"], ["fail", "Something is wrong"]]);
const ROUND_MINUTES = [10, 15];
const JUDGE_PER_RULE = 30;

const clock = (/** @type {number} */ ms) => {
  const sec = Math.max(0, Math.floor(ms / 1000));
  return `${String(Math.floor(sec / 60)).padStart(2, "0")}:${String(sec % 60).padStart(2, "0")}`;
};
const pct = (/** @type {number | null | undefined} */ x) => `${Math.round((x ?? 0) * 100)}%`;

/** @param {import("../router.js").PageContext} ctx */
export async function render(ctx) {
  let kind = "drill";
  const content = h("div", {});
  const tabs = tabList([{ id: "drill", label: "Drill rounds" }, { id: "judge", label: "Judge calibration" }], kind, (id) => { kind = id; void draw(); }, "Calibration sections");
  setChildren(ctx.root, pageHeader({ title: "Calibration", lead: "Short blind rounds, 10 to 15 minutes. Your answers teach the checks what you would approve." }), tabs, content);

  // a round is meant to take 10 to 15 minutes: a quiet clock, never a deadline
  let roundId = "";
  let started = Date.now();
  /** @type {HTMLElement | null} */ let clockEl = null;
  const tick = () => {
    if (!clockEl) return;
    const spent = Date.now() - started;
    clockEl.textContent = `Time so far: ${clock(spent)} (a round takes ${ROUND_MINUTES[0]} to ${ROUND_MINUTES[1]} minutes)` + (spent > ROUND_MINUTES[1] * 60000 ? ". That is long enough: stop here whenever you like." : "");
  };
  const interval = setInterval(tick, 1000);
  ctx.onCleanup(() => clearInterval(interval));

  /** @type {((e: KeyboardEvent) => void) | null} */ let keys = null;
  const dropKeys = () => { if (keys) { document.removeEventListener("keydown", keys); keys = null; } };
  ctx.onCleanup(dropKeys);

  const draw = async () => {
    dropKeys();
    /** @type {{data: any, unavailable: boolean}} */ let res;
    try { res = await tryGet("/api/calibration/session", { query: { kind }, signal: ctx.signal }); } catch (err) {
      if (!ctx.active()) return;
      if (err instanceof ApiError && [403, 409, 423].includes(err.status)) { setChildren(content, locked(Number(err.data?.approved_duos ?? err.data?.approved ?? 0), Number(err.data?.needed ?? NEEDED))); return; }
      setChildren(content, errorNote(friendly(err))); return;
    }
    if (!ctx.active()) return;
    if (res.unavailable) { setChildren(content, notAvailable("Calibration rounds")); return; }
    const d = res.data || {};
    if (d.locked) { setChildren(content, locked(Number(d.approved_duos ?? d.approved ?? 0), Number(d.needed ?? NEEDED))); return; }
    const sess = d.session || null;
    if (sess?.id && sess.id !== roundId) { roundId = sess.id; started = sess.started_at ? Date.parse(sess.started_at) || Date.now() : Date.now(); }
    const items = /** @type {any[]} */ (d.items ?? []);
    const parts = [];
    if (items.length) parts.push(kind === "drill" ? drillCard(d, items[0], sess) : judgeCard(items[0], d));
    else parts.push(done(d, sess));
    parts.push(countsPanel(d));
    setChildren(content, ...parts);
    tick();
  };

  /** Everything the person does on one picture. @param {any} d @param {any} item @param {any} sess */
  function drillCard(d, item, sess) {
    /** @type {string | null} */ let label = null;
    /** @type {boolean | null} */ let like = null;
    const choices = DRILL_CHOICES;
    const answered = Number(sess?.answered ?? 0) + Number(sess?.skipped ?? 0);
    const total = Number(sess?.total ?? 0);
    const err = h("div", { class: "msg" });
    const save = h("button", { type: "button", class: "btn primary big", disabled: true, "aria-keyshortcuts": "Enter" }, "Save and next");
    const labelBtns = choices.map(([v, t], i) => h("button", { type: "button", class: "choice", "aria-pressed": "false", "aria-keyshortcuts": String(i + 1), dataset: { value: v },
      onclick: () => pick(v) }, t));
    const likeBtns = /** @type {[boolean, string, string][]} */ ([[true, "Like", "L"], [false, "Dislike", "D"]]).map(([v, t, k]) => h("button", { type: "button", class: "choice small", "aria-pressed": "false",
      "aria-keyshortcuts": k, dataset: { like: String(v) }, onclick: () => pickLike(v) }, t));
    /** @param {string} v */
    function pick(v) { label = v; labelBtns.forEach((b) => b.setAttribute("aria-pressed", String(b.dataset.value === v))); save.disabled = false; }
    /** @param {boolean} v */
    function pickLike(v) { like = like === v ? null : v; likeBtns.forEach((b) => b.setAttribute("aria-pressed", String(like !== null && b.dataset.like === String(like)))); }
    /** @param {string} answer */
    const send = async (answer) => {
      setChildren(err);
      save.disabled = true;
      try {
        await post("/api/calibration/labels", { item_id: item.item_id, label: answer, like: answer === "skip" ? undefined : (like ?? undefined) });
        await draw();
      } catch (e) {
        if (e instanceof ApiError && ["already_answered", "unknown_item", "session_closed", "cap_reached"].includes(e.code)) { toast(friendly(e), { kind: "info" }); await draw(); return; }
        setChildren(err, errorNote(friendly(e)));
        save.disabled = label === null;
      }
    };
    save.addEventListener("click", () => { if (label) void send(label); });
    keys = (e) => {
      if (e.ctrlKey || e.metaKey || e.altKey || /** @type {HTMLElement} */ (e.target)?.closest?.("input, textarea, select")) return;
      const n = Number(e.key);
      if (n >= 1 && n <= choices.length) pick(choices[n - 1][0]);
      else if (e.key.toLowerCase() === "l") pickLike(true);
      else if (e.key.toLowerCase() === "d") pickLike(false);
      else if (e.key === "Enter" && label && !(/** @type {HTMLElement} */ (e.target)?.closest?.("button"))) { e.preventDefault(); save.click(); }
    };
    document.addEventListener("keydown", keys);
    clockEl = h("p", { class: "muted small", "aria-live": "off" });
    return panel({ class: "drill" },
      h("div", { class: "drill-top" },
        h("p", { class: "drill-count" }, total ? `Picture ${Math.min(answered + 1, total)} of ${total}` : "Picture"),
        total ? progress(Math.min(1, answered / total), "Round progress") : null,
        h("button", { type: "button", class: "btn quiet small", onclick: async () => { try { await post("/api/calibration/session/end"); } catch (e) { toast(friendly(e), { kind: "bad" }); } await draw(); } }, "Stop here")),
      clockEl,
      h("div", { class: "drill-stage" },
        h("figure", { class: "drill-pic" }, casImage(String(item.images?.[0] ?? ""), { alt: "Two blocky characters, each shown from the front and from the back" })),
        h("div", { class: "drill-controls" },
          h("p", { class: "drill-question" }, String(item.question || "What is this?")),
          h("div", { class: "answer-group", role: "group", "aria-label": "Your answer" }, h("div", { class: "choice-row" }, labelBtns)),
          h("div", { class: "answer-group", role: "group", "aria-label": "Do you like it?" }, h("p", { class: "answer-label" }, "Do you like it? (optional)"), h("div", { class: "choice-row" }, likeBtns)),
          err,
          h("div", { class: "drill-actions" }, save, h("button", { type: "button", class: "btn quiet", onclick: () => void send("skip") }, "Skip this one")),
          h("p", { class: "muted small" }, "Keys: 1 2 3 to answer, L or D to like, Enter to save"))),
      d.counts?.cap_reached ? note("This is the last one: practice answers are capped at a quarter of all answers.", "info") : null);
  }

  /** @param {any} item @param {any} d */
  function judgeCard(item, d) {
    const err = h("div", { class: "msg" });
    clockEl = null;
    /** @param {string} answer */
    const send = async (answer) => {
      setChildren(err);
      try { await post("/api/calibration/labels", { item_id: item.item_id, label: answer }); await draw(); } catch (e) {
        if (e instanceof ApiError && ["already_answered", "unknown_item"].includes(e.code)) { toast(friendly(e), { kind: "info" }); await draw(); return; }
        setChildren(err, errorNote(friendly(e)));
      }
    };
    return panel({ class: "drill" },
      h("div", { class: "drill-stage" },
        h("figure", { class: "drill-pic" }, casImage(String(item.images?.[0] ?? ""), { alt: "A picture made for one of your duos" })),
        h("div", { class: "drill-controls" },
          h("p", { class: "drill-question" }, String(item.question || "Is anything wrong with this?")),
          h("div", { class: "answer-group", role: "group", "aria-label": "Your answer" }, h("div", { class: "choice-row" },
            JUDGE_CHOICES.map(([v, t]) => h("button", { type: "button", class: "choice", onclick: () => void send(v) }, t)))),
          err,
          h("div", { class: "drill-actions" }, h("button", { type: "button", class: "btn quiet", onclick: () => void send("skip") }, "Skip this one")),
          d.message ? h("p", { class: "muted small" }, d.message) : null)));
  }

  /** @param {any} d @param {any} sess */
  function done(d, sess) {
    clockEl = null;
    if (kind === "judge") return emptyState("Nothing to look at", String(d.message || "The checks have not judged any picture of yours yet. Come back after a duo has been made."));
    if (d.cap_reached) {
      return panel({ class: "round-done" }, h("h2", {}, "That is enough practice for now"),
        h("p", { class: "muted" }, "Practice answers are capped at a quarter of all your answers, so they never drown out your real choices. Approve a few more duos and come back."),
        h("a", { class: "btn primary", href: "#/new" }, "Make a duo"));
    }
    return panel({ class: "round-done" }, h("h2", {}, sess?.finished ? "That is the end of this round" : "All done for now"),
      h("p", { class: "muted" }, sess?.finished ? `Thank you. You answered ${plural(Number(sess.answered ?? 0), "picture")}.` : String(d.message || "There is nothing left to look at.")),
      h("button", { type: "button", class: "btn primary", onclick: () => void draw() }, "Start another round"));
  }

  await draw();
}

/** @param {any} d */
function countsPanel(d) {
  const c = d.counts;
  if (!c) return null;
  if (d.kind === "judge") {
    const rules = Object.entries(c.labels_per_rule ?? {});
    return panel({ title: "What you have judged" }, rules.length
      ? h("ul", { class: "count-lines" }, rules.map(([rule, n]) => h("li", {}, h("span", {}, c.rule_titles?.[rule] || rule.replace(/_/g, " ")), progress(Math.min(1, Number(n) / JUDGE_PER_RULE), rule), h("span", { class: "bar-value" }, String(n)))))
      : h("p", { class: "muted" }, "Nothing yet."), h("p", { class: "muted small" }, `A few dozen answers per rule is enough: about ${JUDGE_PER_RULE}.`));
  }
  const by = c.by_source ?? {};
  const real = Number(by.gate ?? 0) + Number(by.calibration ?? 0);
  const practice = Number(by.drill ?? 0);
  return panel({ title: "How many answers there are" },
    h("ul", { class: "count-lines" },
      h("li", {}, h("span", {}, "Your real choices at the gates"), progress(Math.min(1, real / Math.max(1, Number(c.target ?? 200))), "Real choices"), h("span", { class: "bar-value" }, String(real))),
      h("li", {}, h("span", {}, "Practice rounds"), progress(Math.min(1, Number(c.drill_share ?? 0) / Math.max(0.01, Number(c.cap ?? 0.25))), "Practice share of the cap"), h("span", { class: "bar-value" }, String(practice))),
      h("li", {}, h("span", {}, "Enough to suggest new limits"), progress(Math.min(1, Number(c.progress ?? 0)), "Progress to the answers needed"), h("span", { class: "bar-value" }, pct(c.progress)))),
    h("p", { class: "muted small" }, `Practice rounds are capped at a quarter of all answers (now ${pct(c.drill_share)}), so they never drown out your real choices. `
      + `About ${c.target ?? 200} answers are needed before DuoSkin suggests where its limits should sit.`));
}

/** @param {number} approved @param {number} [needed] */
function locked(approved, needed = NEEDED) {
  return panel({}, h("h2", {}, "Not unlocked yet"), h("p", {}, `Calibration needs at least ${needed} approved duos so it has your real choices to compare with. You have ${approved}.`),
    progress(Math.min(1, approved / needed), "Approved duos"), h("a", { class: "btn primary", href: "#/new" }, "Make a duo"));
}
