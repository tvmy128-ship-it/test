// @ts-check
// The only place that talks to the server: a tiny fetch wrapper.
//  * the per-launch token comes from <meta name="duoskin-token"> and goes in X-DuoSkin-Token on EVERY request;
//  * answers are JSON; errors become ApiError with a plain-English message (never raw JSON or a stack trace);
//  * a "bad_token" answer means the server was restarted: the page reloads itself once.

/** @returns {string} */
function readToken() {
  const meta = /** @type {HTMLMetaElement | null} */ (document.querySelector('meta[name="duoskin-token"]'));
  return meta?.content || "";
}
export const TOKEN = readToken();
export const TOKEN_MISSING = !TOKEN || TOKEN === "__DUOSKIN_TOKEN__";

/** Headers for a hand-made fetch() (a model file, say): every request carries the token. */
export function authHeaders() { return { "X-DuoSkin-Token": TOKEN }; }

/** Plain-English wording for the error codes the API answers with (APP_SPEC 8.6, 9.1, 13). */
const FRIENDLY = {
  bad_token: "This page is out of date because DuoSkin Studio was restarted. Reloading it now.",
  bad_origin: "This request did not come from the DuoSkin Studio page, so it was refused.",
  not_implemented: "This part of the app is not available yet.",
  not_found: "That could not be found. It may have been removed.",
  version_conflict: "This changed a moment ago (maybe in another tab). The latest version is showing now.",
  gate_closed: "This decision was already made, so nothing was changed.",
  provisional_pending: "Finish or go back on your earlier choice for this tile first.",
  action_not_allowed: "That action is not available for this item right now.",
  unknown_tile: "That item is no longer on the page. Refresh to see the latest.",
  bad_target: "Please pick which part you mean.",
  wrong_stage: "That cannot be done at this stage of the duo.",
  plan_running: "A plan is already being made for this duo.",
  settings_locked: "These settings can only be changed before planning starts.",
  too_many_references: "A duo can have at most 4 reference pictures.",
  too_large: "That file is too large.",
  bad_image: "That file is not a PNG, JPEG or WebP picture.",
  validation: "Some of the details need fixing before this can go ahead.",
  bad_key: "That key was not accepted. Check that you copied all of it.",
  bad_settings: "One of those values is not allowed, so nothing was changed. Check the numbers and names you typed.",
  server_error: "Something went wrong inside DuoSkin Studio. Nothing was charged for it. The details are in the log file.",
  not_failed: "That step is not in a failed state, so it cannot be retried.",
};

export class ApiError extends Error {
  /**
   * @param {string} message plain-English text, safe to show
   * @param {{status?: number, code?: string, data?: any}} [info]
   */
  constructor(message, info = {}) {
    super(message);
    this.name = "ApiError";
    this.status = info.status ?? 0;
    this.code = info.code ?? "";
    this.data = info.data ?? null;
  }

  /** The route is not built yet (501) or does not exist (a bare 404): show a "not available yet" state. */
  get unavailable() {
    return this.status === 501 || (this.status === 404 && (this.code === "http_404" || this.code === ""));
  }

  get conflict() { return this.status === 409; }
}

const TECHNICAL = /traceback|file "|\bat 0x|<html|^[[{]|sqlite|errno|\.py\b/i;
const ALWAYS_MAPPED = new Set(["bad_token", "server_error", "not_implemented", "validation", "version_conflict", "gate_closed", "bad_settings"]);

/** @param {any} data @param {number} status */
function messageFor(data, status) {
  const code = data && typeof data === "object" ? String(data.error || "") : "";
  const raw = data && typeof data === "object" ? String(data.message || "") : "";
  const mapped = /** @type {Record<string, string>} */ (FRIENDLY)[code];
  if (mapped && ALWAYS_MAPPED.has(code)) return mapped;
  if (raw && raw.length < 300 && !TECHNICAL.test(raw)) return raw;
  if (mapped) return mapped;
  if (status === 404) return FRIENDLY.not_found;
  if (status >= 500) return FRIENDLY.server_error;
  return "That did not work. Please try again.";
}

/**
 * Turn anything thrown into text for the user.
 * @param {unknown} err
 */
export function friendly(err) {
  if (err instanceof ApiError) return err.message;
  if (err instanceof DOMException && err.name === "AbortError") return "Cancelled.";
  if (err instanceof TypeError) return "Cannot reach DuoSkin Studio. Is the black window (start.bat) still open?";
  return "Something unexpected happened. Please try again.";
}

let reloading = false;
function reloadOnce() {
  if (reloading) return;
  let last = 0;
  try { last = Number(sessionStorage.getItem("duoskin-reload") || 0); } catch { /* storage blocked */ }
  if (Date.now() - last < 10000) return;   // never loop
  reloading = true;
  try { sessionStorage.setItem("duoskin-reload", String(Date.now())); } catch { /* storage blocked */ }
  location.reload();
}

/**
 * @typedef {object} RequestOptions
 * @property {any} [body] JSON body (objects) or FormData
 * @property {Record<string, string | number | boolean | null | undefined>} [query]
 * @property {AbortSignal} [signal]
 */

/**
 * @param {string} method
 * @param {string} path  e.g. "/api/state"
 * @param {RequestOptions} [opts]
 * @returns {Promise<any>} parsed JSON, or null for 204
 */
export async function api(method, path, opts = {}) {
  let url = path;
  if (opts.query) {
    const qs = new URLSearchParams();
    for (const [k, v] of Object.entries(opts.query)) if (v !== undefined && v !== null && v !== "") qs.set(k, String(v));
    const s = qs.toString();
    if (s) url += (url.includes("?") ? "&" : "?") + s;
  }
  /** @type {Record<string, string>} */
  const headers = { "X-DuoSkin-Token": TOKEN, Accept: "application/json" };
  /** @type {RequestInit} */
  const init = { method, headers, signal: opts.signal, cache: "no-store" };
  if (opts.body instanceof FormData) init.body = opts.body;
  else if (opts.body !== undefined) {
    headers["Content-Type"] = "application/json";
    init.body = JSON.stringify(opts.body);
  }
  const res = await fetch(url, init);
  const text = await res.text();
  /** @type {any} */ let data = null;
  if (text) { try { data = JSON.parse(text); } catch { data = null; } }
  if (!res.ok) {
    if (res.status === 403 && data?.error === "bad_token") reloadOnce();
    throw new ApiError(messageFor(data, res.status), { status: res.status, code: String(data?.error || ""), data });
  }
  return data;
}

/** @param {string} path @param {RequestOptions} [o] */
export const get = (path, o) => api("GET", path, o);
/** @param {string} path @param {any} [body] @param {RequestOptions} [o] */
export const post = (path, body, o) => api("POST", path, { ...o, body: body ?? undefined });
/** @param {string} path @param {any} [body] @param {RequestOptions} [o] */
export const put = (path, body, o) => api("PUT", path, { ...o, body });
/** @param {string} path @param {any} [body] @param {RequestOptions} [o] */
export const patch = (path, body, o) => api("PATCH", path, { ...o, body });
/** @param {string} path @param {RequestOptions} [o] */
export const del = (path, o) => api("DELETE", path, o);

/**
 * GET that turns "not built yet" (404 route / 501) into `{unavailable: true}` instead of throwing.
 * @param {string} path @param {RequestOptions} [o]
 * @returns {Promise<{data: any, unavailable: boolean}>}
 */
export async function tryGet(path, o) {
  try {
    return { data: await get(path, o), unavailable: false };
  } catch (err) {
    if (err instanceof ApiError && err.unavailable) return { data: null, unavailable: true };
    throw err;
  }
}

// ------------------------------------------------------------------------------------------------ content-addressed files
/** @type {Map<string, string>} the extension that worked for a sha (the API gives bare shas; /cas/ needs `<sha>.<ext>`) */
const KNOWN_EXT = new Map();
const EXT_ORDER = ["png", "webp", "jpeg", "svg"];

/** A hint from a role name. @param {string} role */
export function extForRole(role) {
  if (/(^|_)(glb|mesh|model)(_|$)/i.test(role)) return "glb";
  if (/svg|vector/i.test(role)) return "svg";
  return "png";
}

/** @param {string} sha @param {string} [ext] */
export function casUrl(sha, ext) {
  if (/\.[a-z0-9]{2,5}$/i.test(sha)) return `/cas/${sha}`;
  return `/cas/${sha}.${ext || KNOWN_EXT.get(sha) || "png"}`;
}

/**
 * An <img> for a CAS sha. When the guessed extension is wrong the server answers 404 and the image tries the other
 * picture types once each (the extension that worked is remembered for the session).
 * @param {string} sha
 * @param {{alt?: string, className?: string, ext?: string, onLoad?: (img: HTMLImageElement) => void}} [o]
 */
export function casImage(sha, o = {}) {
  const img = document.createElement("img");
  img.alt = o.alt ?? "";
  img.decoding = "async";
  if (o.className) img.className = o.className;
  const order = [o.ext || KNOWN_EXT.get(sha) || "png", ...EXT_ORDER].filter((e, i, a) => a.indexOf(e) === i);
  let i = 0;
  img.addEventListener("error", () => {
    i += 1;
    if (i < order.length) img.src = `/cas/${sha}.${order[i]}`;
    else img.classList.add("broken");
  });
  img.addEventListener("load", () => {
    KNOWN_EXT.set(sha, order[i]);
    o.onLoad?.(img);
  });
  img.src = `/cas/${sha}.${order[0]}`;
  return img;
}
