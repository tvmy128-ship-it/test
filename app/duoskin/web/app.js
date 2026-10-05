// @ts-check
// DuoSkin Studio: the page shell. Boots the state snapshot and the live event stream, draws the top bar (stage bar, cost
// bar, demo banner, connection state) and starts the hash router. Every page lives in pages/*.js.
//
// Security contract (APP_SPEC 13, security.py): the launch token is read from <meta name="duoskin-token"> by api.js and
// sent as X-DuoSkin-Token on every request; there is one EventSource per browser (the leader tab, events.js); no inline
// scripts or styles anywhere, so the strict Content-Security-Policy holds.
import { TOKEN_MISSING, post, friendly } from "./api.js";
import { h, $, setChildren } from "./dom.js";
import { boot, state, subscribe, setProject } from "./store.js";
import { onStatus, on } from "./events.js";
import { onRoute, start } from "./router.js";
import { renderStageBar } from "./components/stagebar.js";
import { renderCostBar } from "./components/costbar.js";
import { confirmDialog } from "./components/modal.js";
import { toast } from "./components/toast.js";
import { maybeConfirmPalette } from "./components/paletteconfirm.js";

const main = /** @type {HTMLElement} */ ($("#main"));

function paintShell() {
  const snap = state.snapshot;
  const demo = Boolean(state.health?.demo ?? snap?.demo);
  const banner = $("#demo-banner");
  if (banner) banner.hidden = !demo;
  const bundle = state.project;
  const project = bundle ? bundle.project : null;
  const waiting = project ? (snap?.open_gates ?? []).filter((g) => g.project_id === project.id).length : (snap?.open_gates.length ?? 0);
  const stageHost = $("#stagebar");
  if (stageHost) renderStageBar(stageHost, project, project ? waiting : 0);
  const costHost = $("#costbar");
  if (costHost) renderCostBar(costHost, { demo, todayUsd: snap?.today_usd ?? 0, project, queue: snap?.queue, doctor: snap?.doctor, waiting: project ? 0 : waiting, running: (snap?.steps?.running ?? 0) + (snap?.steps?.waiting_remote ?? 0) });
  const notice = $("#notice-banner");
  if (notice && state.offline) { notice.hidden = false; notice.textContent = state.error || "Cannot reach DuoSkin Studio."; }
  else if (notice && notice.dataset.sticky !== "1") notice.hidden = true;
}

/** @param {import("./events.js").Connection} c */
function paintConnection(c) {
  const el = $("#conn");
  if (!el) return;
  el.hidden = c === "live" || c === "connecting";
  el.className = "pill " + (c === "offline" ? "bad" : "warn");
  el.textContent = c === "polling" ? "Updating every 2 seconds" : c === "offline" ? "Offline" : "Reconnecting…";
}

/** @param {import("./router.js").ParsedRoute} r */
function paintNav(r) {
  for (const a of document.querySelectorAll(".mainnav a")) {
    const key = /** @type {HTMLElement} */ (a).dataset.nav || "";
    const on = key === "/" ? r.path === "/" || r.path.startsWith("/p/") || r.path === "/new" : r.path === key || r.path.startsWith(key + "/");
    if (on) a.setAttribute("aria-current", "page"); else a.removeAttribute("aria-current");
  }
  const sub = $(".subbar");
  if (sub) sub.classList.toggle("has-project", Boolean(r.params.id));
  const id = r.params.id || null;
  void setProject(id);
  const live = $("#announce");
  if (live) live.textContent = r.route ? `${r.route.title}` : "Page not found";
}

async function main_() {
  if (TOKEN_MISSING) {
    setChildren(main, h("section", { class: "panel panel-error" }, h("h1", {}, "Open DuoSkin Studio from its own address"),
      h("p", {}, "This page was not opened by DuoSkin Studio, so it has no key to talk to the program. Close it, then open the address shown in the black start.bat window.")));
    return;
  }
  // keep focused and scrolled-to elements out from under the sticky header (its height changes with the banner and stage bar)
  const top = $(".shell-top");
  if (top && typeof ResizeObserver !== "undefined") {
    new ResizeObserver(() => document.documentElement.style.setProperty("--shell-h", `${top.offsetHeight}px`)).observe(top);
  }
  subscribe(paintShell);
  onStatus(paintConnection);
  onRoute(paintNav);
  on("toast", (e) => toast(String(e.payload.message || ""), { kind: e.payload.level === "warning" ? "warn" : e.payload.level === "error" ? "bad" : "info" }));
  on("budget.low", (e) => toast(String(e.payload.message || "You are close to the budget cap for this duo."), { kind: "warn" }));
  on("server.restarting", () => {
    const n = $("#notice-banner");
    if (n) { n.hidden = false; n.dataset.sticky = "1"; n.textContent = "DuoSkin Studio is restarting. This page will reconnect by itself."; }
  });
  // after a Gate 1 approval a small "which colours?" question may open (APP_SPEC 9.2): answer it wherever the user is
  on("gate.opened", (e) => { if (e.project_id) void maybeConfirmPalette(e.project_id); });
  on("resync", () => { const n = $("#notice-banner"); if (n && n.dataset.sticky) { delete n.dataset.sticky; n.hidden = true; } });
  $("#quit")?.addEventListener("click", async () => {
    const ok = await confirmDialog({ title: "Quit DuoSkin Studio?", message: "Anything running is stopped safely and your work is saved. You can start it again from start.bat.", confirmLabel: "Quit", tone: "danger" });
    if (!ok) return;
    try { await post("/api/shutdown"); } catch (err) { toast(friendly(err), { kind: "bad" }); return; }
    setChildren(document.body, h("main", { class: "goodbye" }, h("h1", {}, "DuoSkin Studio has stopped"), h("p", {}, "You can close this tab. To use it again, open start.bat.")));
  });
  // a plain #main link would change the hash and the router would treat it as a page, so the skip link focuses <main> itself
  $(".skip-link")?.addEventListener("click", (e) => { e.preventDefault(); main.focus(); });
  await boot();
  paintShell();
  start(main);
}

main_().catch((err) => {
  console.error(err);
  setChildren(main, h("section", { class: "panel panel-error", role: "alert" }, h("h1", {}, "DuoSkin Studio could not start"), h("p", {}, friendly(err))));
});
