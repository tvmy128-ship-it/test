// @ts-check
// Hash router: "#/p/<id>/gate1?x=1" -> a page module in pages/. Each page exports `render(ctx)` and may return a cleanup
// function. The context carries what a page needs (params, query, an abort signal, live-event helpers) and everything it
// registers through `ctx` is torn down when the user leaves.
import { on as onEvent } from "./events.js";
import { friendly } from "./api.js";
import { h, debounce, setChildren } from "./dom.js";

/**
 * @typedef {object} PageContext
 * @property {Record<string, string>} params route parameters, e.g. {id: "prj_1"}
 * @property {URLSearchParams} query
 * @property {string} path
 * @property {HTMLElement} root the element the page renders into
 * @property {AbortSignal} signal aborted when the user leaves the page
 * @property {(title: string) => void} setTitle
 * @property {(fn: () => void) => void} onCleanup
 * @property {(type: string, fn: (e: import("./events.js").DuoEvent) => void) => void} on live event, removed on leave
 * @property {(types: string[], fn: () => void, ms?: number) => void} live call `fn` (debounced) when any of these events
 *   arrives for this page's project (or after a reconnect)
 * @property {(path: string, opts?: {replace?: boolean}) => void} navigate
 * @property {() => boolean} active false once the user left (stop async work)
 */

/** @typedef {{pattern: string, page: string, title: string}} RouteDef */

/** @type {RouteDef[]} */
export const ROUTES = [
  { pattern: "/", page: "home", title: "Your duos" },
  { pattern: "/setup", page: "setup", title: "Set up" },
  { pattern: "/settings", page: "settings", title: "Settings" },
  { pattern: "/settings/:tab", page: "settings", title: "Settings" },
  { pattern: "/new", page: "brief", title: "New duo" },
  { pattern: "/p/:id", page: "project", title: "Your duo" },
  { pattern: "/p/:id/plan", page: "plan", title: "The plan" },
  { pattern: "/p/:id/gate1", page: "gate1", title: "Pick a concept" },
  { pattern: "/p/:id/board", page: "board", title: "The parts" },
  { pattern: "/p/:id/build", page: "build", title: "Build" },
  { pattern: "/p/:id/gate3", page: "gate3", title: "Pick the final duo" },
  { pattern: "/p/:id/export", page: "export", title: "Export" },
  { pattern: "/jobs", page: "jobs", title: "Jobs" },
  { pattern: "/costs", page: "costs", title: "Costs" },
  { pattern: "/library", page: "library", title: "Library" },
  { pattern: "/calibration", page: "calibration", title: "Calibration" },
  { pattern: "/learning", page: "learning", title: "Learning" },
];

/** @param {string} pattern */
function compile(pattern) {
  const names = /** @type {string[]} */ ([]);
  const src = pattern.replace(/[.*+?^${}()|[\]\\]/g, "\\$&").replace(/:([a-z]+)/g, (_m, n) => { names.push(n); return "([^/]+)"; });
  return { re: new RegExp(`^${src}/?$`), names };
}
const COMPILED = ROUTES.map((r) => ({ ...r, ...compile(r.pattern) }));

/** @param {string} hash e.g. "#/p/x/plan?a=1" */
export function parseHash(hash) {
  const raw = hash.replace(/^#/, "") || "/";
  const [pathRaw, qs = ""] = raw.split("?");
  const path = pathRaw.startsWith("/") ? pathRaw : "/" + pathRaw;
  for (const r of COMPILED) {
    const m = r.re.exec(path);
    if (m) {
      /** @type {Record<string, string>} */
      const params = {};
      r.names.forEach((n, i) => { params[n] = decodeURIComponent(m[i + 1]); });
      return { route: /** @type {RouteDef} */ (r), params, query: new URLSearchParams(qs), path };
    }
  }
  return { route: null, params: {}, query: new URLSearchParams(qs), path };
}

/** @typedef {ReturnType<typeof parseHash>} ParsedRoute */

/** @type {Set<(r: ParsedRoute) => void>} */
const routeListeners = new Set();
/** @type {ParsedRoute} */
let current = parseHash(location.hash);
/** @type {(() => void)[]} */
let cleanups = [];
/** @type {AbortController | null} */
let controller = null;
/** @type {HTMLElement | null} */
let container = null;
let renderToken = 0;

export function currentRoute() { return current; }

/** Called after every navigation (the stage bar and cost bar follow the open project). @param {(r: ParsedRoute) => void} fn */
export function onRoute(fn) {
  routeListeners.add(fn);
  fn(current);
  return () => { routeListeners.delete(fn); };
}

/** @param {string} path "/p/x/plan" @param {{replace?: boolean}} [opts] */
export function navigate(path, opts = {}) {
  const hash = "#" + (path.startsWith("/") ? path : "/" + path);
  if (opts.replace) history.replaceState(null, "", hash);
  if (location.hash === hash && !opts.replace) { void renderCurrent(); return; }
  if (opts.replace) void renderCurrent();
  else location.hash = hash;
}

function teardown() {
  controller?.abort();
  for (const fn of cleanups.splice(0)) {
    try { fn(); } catch (err) { console.error("page cleanup failed", err); }
  }
}

/** @param {string} title */
function setDocumentTitle(title) {
  document.title = title ? `${title} · DuoSkin Studio` : "DuoSkin Studio";
}

function loadingPanel() {
  return h("div", { class: "page-loading", role: "status" }, h("span", { class: "spinner", "aria-hidden": "true" }), "Loading…");
}

/** @param {Error} err @param {() => void} retry */
function failurePanel(err, retry) {
  console.error(err);
  return h("section", { class: "panel panel-error", role: "alert" },
    h("h1", { tabindex: "-1" }, "This page ran into a problem"),
    h("p", {}, friendly(err)),
    h("p", { class: "muted" }, "Nothing was charged. The technical details are in your browser console and the DuoSkin log file."),
    h("button", { type: "button", class: "btn primary", onclick: retry }, "Try again"));
}

async function renderCurrent() {
  if (!container) return;
  const token = ++renderToken;
  teardown();
  current = parseHash(location.hash);
  for (const fn of [...routeListeners]) fn(current);
  controller = new AbortController();
  const signal = controller.signal;
  const root = container;
  setChildren(root, loadingPanel());
  const { route, params, query, path } = current;
  if (!route) {
    setDocumentTitle("Not found");
    setChildren(root, h("section", { class: "panel" }, h("h1", { tabindex: "-1" }, "That page does not exist"),
      h("p", {}, "The link may be out of date."), h("a", { class: "btn primary", href: "#/" }, "Back to your duos")));
    focusHeading(root);
    return;
  }
  setDocumentTitle(route.title);
  /** @type {PageContext} */
  const ctx = {
    params, query, path, root, signal,
    setTitle: (t) => { if (token === renderToken) setDocumentTitle(t); },
    onCleanup: (fn) => { cleanups.push(fn); },
    on: (type, fn) => { cleanups.push(onEvent(type, (e) => { if (token === renderToken) fn(e); })); },
    live: (types, fn, ms = 250) => {
      const run = debounce(() => { if (token === renderToken) fn(); }, ms);
      cleanups.push(() => run.cancel());
      const mine = (/** @type {import("./events.js").DuoEvent} */ e) => !params.id || !e.project_id || e.project_id === params.id;
      for (const type of [...types, "resync"]) cleanups.push(onEvent(type, (e) => { if (type === "resync" || mine(e)) run(); }));
    },
    navigate,
    active: () => token === renderToken && !signal.aborted,
  };
  try {
    const mod = await import(`./pages/${route.page}.js`);
    if (token !== renderToken) return;
    setChildren(root);
    const cleanup = await mod.render(ctx);
    if (token !== renderToken) { if (typeof cleanup === "function") cleanup(); return; }
    if (typeof cleanup === "function") cleanups.push(cleanup);
    focusHeading(root);
  } catch (err) {
    if (token !== renderToken) return;
    setChildren(root, failurePanel(/** @type {Error} */ (err), () => { void renderCurrent(); }));
    focusHeading(root);
  }
}

/** Keyboard and screen-reader users land on the page heading after a navigation. @param {HTMLElement} root */
function focusHeading(root) {
  const heading = /** @type {HTMLElement | null} */ (root.querySelector("h1"));
  if (heading) {
    heading.setAttribute("tabindex", "-1");
    heading.focus({ preventScroll: true });
  }
  window.scrollTo({ top: 0 });
}

/** @param {HTMLElement} el the <main> element */
export function start(el) {
  container = el;
  window.addEventListener("hashchange", () => { void renderCurrent(); });
  void renderCurrent();
}
