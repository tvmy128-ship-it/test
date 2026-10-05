// @ts-check
// App-wide state: the /api/state snapshot (projects, jobs, open gates, queue, doctor) and the open project's bundle.
// Kept fresh by the event stream: events only trigger a debounced re-fetch, so the server stays the single source of truth.
import { get, tryGet, friendly } from "./api.js";
import { on, start as startEvents } from "./events.js";
import { debounce } from "./dom.js";

/**
 * @typedef {object} Snapshot
 * @property {string} version
 * @property {boolean} demo
 * @property {number} max_event_id
 * @property {any[]} projects  ProjectSummary
 * @property {any[]} jobs      JobView
 * @property {Record<string, number>} steps
 * @property {{id: string, project_id: string, kind: string, tiles: number, job_id: string}[]} open_gates
 * @property {{paused: boolean | string | null, paused_providers: Record<string, string>, scheduler_running: boolean, paid_blocked: string | null}} queue
 * @property {{summary: any, blocks_paid_features: boolean} | null} doctor
 * @property {number} today_usd
 */

/**
 * @typedef {object} ProjectBundle
 * @property {any} project
 * @property {any[]} parts
 * @property {any | null} spec
 * @property {any | null} dna_card
 * @property {any[]} open_gates
 */

/** @type {{snapshot: Snapshot | null, health: {ok: boolean, version: string, demo: boolean} | null, project: ProjectBundle | null, projectId: string | null, error: string, offline: boolean}} */
export const state = { snapshot: null, health: null, project: null, projectId: null, error: "", offline: false };

/** @type {Set<(s: typeof state) => void>} */
const subscribers = new Set();

/** @param {(s: typeof state) => void} fn */
export function subscribe(fn) {
  subscribers.add(fn);
  return () => { subscribers.delete(fn); };
}
function notify() { for (const fn of [...subscribers]) { try { fn(state); } catch (e) { console.error(e); } } }

export async function refreshSnapshot() {
  try {
    state.snapshot = await get("/api/state");
    state.offline = false;
    state.error = "";
  } catch (err) {
    state.offline = true;
    state.error = friendly(err);
  }
  notify();
  return state.snapshot;
}

export async function refreshHealth() {
  try {
    state.health = await get("/api/health");
  } catch {
    state.health = null;
  }
  notify();
}

/** Load (or reload) the bundle of the project the user is looking at. @param {string | null} id */
export async function setProject(id) {
  if (id !== state.projectId) { state.projectId = id; state.project = null; notify(); }
  if (!id) return null;
  return reloadProject();
}

export async function reloadProject() {
  const id = state.projectId;
  if (!id) return null;
  try {
    const bundle = await get(`/api/projects/${encodeURIComponent(id)}`);
    if (state.projectId === id) { state.project = bundle; notify(); }
    return bundle;
  } catch {
    if (state.projectId === id) { state.project = null; notify(); }
    return null;
  }
}

const refreshSoon = debounce(() => { void refreshSnapshot(); }, 300);
const reloadProjectSoon = debounce(() => { void reloadProject(); }, 300);

/** Start the snapshot-and-tail flow once at boot. */
export async function boot() {
  await Promise.all([refreshHealth(), refreshSnapshot()]);
  startEvents(state.snapshot?.max_event_id ?? 0);
  const snapshotTypes = ["step.state", "job.state", "gate.opened", "gate.updated", "project.stage", "part.state", "cost.added", "tile.updated", "budget.low"];
  for (const t of snapshotTypes) {
    on(t, (e) => {
      refreshSoon();
      if (e.project_id && e.project_id === state.projectId) reloadProjectSoon();
    });
  }
  on("cost.added", (e) => {
    // show the new spend at once; the debounced reload then replaces it with the ledger's number
    if (state.snapshot && typeof e.payload.usd === "number") { state.snapshot.today_usd += e.payload.usd; }
    if (state.project && e.project_id === state.projectId && typeof e.payload.usd === "number") state.project.project.spent_usd += e.payload.usd;
    notify();
  });
  on("doctor.result", () => refreshSoon());
  on("resync", () => { void refreshHealth(); refreshSoon(); reloadProjectSoon(); });
  setInterval(() => { void refreshHealth(); }, 20000);
}

/** The open gates of one project from the snapshot. @param {string} projectId */
export function gatesOf(projectId) {
  return (state.snapshot?.open_gates ?? []).filter((g) => g.project_id === projectId);
}

/**
 * GET the open gates of a project, newest last. `unavailable` when the gate routes are not built.
 * @param {string} projectId @param {string} [stateName]
 */
export async function loadGates(projectId, stateName = "open") {
  const res = await tryGet("/api/gates", { query: { project_id: projectId, state: stateName } });
  return { gates: /** @type {any[]} */ (res.data || []), unavailable: res.unavailable };
}
