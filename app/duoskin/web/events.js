// @ts-check
// Live updates (APP_SPEC 8.7): "snapshot + tail".
//  * ONE tab holds the EventSource (the leader, elected with the Web Locks API) and relays every event to the other
//    tabs through BroadcastChannel("duoskin"): browsers allow only 6 connections per host across all tabs.
//  * If the stream drops, it is reopened with ?after=<last id> (and EventSource itself sends Last-Event-ID on its own
//    automatic reconnects). After every reconnect a synthetic "resync" event tells the pages to reload their data.
//  * Fallback without Web Locks or EventSource: poll /api/events/poll?after=<id> every 2 seconds.
import { get, TOKEN } from "./api.js";

/**
 * @typedef {object} DuoEvent
 * @property {number} id
 * @property {string} ts
 * @property {string | null} project_id
 * @property {string} type
 * @property {Record<string, any>} payload
 */

/** @typedef {"connecting" | "live" | "reconnecting" | "polling" | "offline"} Connection */

const CHANNEL = "duoskin";
const LOCK = "duoskin-sse";
const POLL_MS = 2000;

/** @type {Map<string, Set<(e: DuoEvent) => void>>} */
const listeners = new Map();
/** @type {Set<(c: Connection) => void>} */
const statusListeners = new Set();
/** @type {Connection} */
let connection = "connecting";
let lastId = 0;
let started = false;
/** @type {BroadcastChannel | null} */
let channel = null;
let isLeader = false;
/** @type {EventSource | null} */
let source = null;
/** @type {any} */
let reopenTimer = null;
let backoff = 1000;
let hadConnection = false;

/** Subscribe to one event type ("*" = all). Returns an unsubscribe function.
 * @param {string} type @param {(e: DuoEvent) => void} fn */
export function on(type, fn) {
  let set = listeners.get(type);
  if (!set) listeners.set(type, (set = new Set()));
  set.add(fn);
  return () => { set.delete(fn); };
}

/** @param {(c: Connection) => void} fn */
export function onStatus(fn) {
  statusListeners.add(fn);
  fn(connection);
  return () => { statusListeners.delete(fn); };
}

export function getConnection() { return connection; }
export function getLastId() { return lastId; }
export function amLeader() { return isLeader; }

/** @param {Connection} next @param {boolean} [relay] */
function setConnection(next, relay = true) {
  if (next === connection) return;
  connection = next;
  for (const fn of [...statusListeners]) fn(next);
  if (relay && isLeader) channel?.postMessage({ kind: "status", status: next });
}

/** @param {DuoEvent} e */
function dispatch(e) {
  for (const key of [e.type, "*"]) {
    for (const fn of [...(listeners.get(key) ?? [])]) {
      try { fn(e); } catch (err) { console.error("event handler failed", err); }
    }
  }
}

/** @param {DuoEvent} e @param {boolean} relay */
function accept(e, relay) {
  if (typeof e.id === "number" && e.id > 0) {
    if (e.id <= lastId) return;   // already seen (a replay after a reconnect)
    lastId = e.id;
  }
  dispatch(e);
  if (relay) channel?.postMessage({ kind: "event", event: e });
}

function resync() {
  const e = { id: 0, ts: new Date().toISOString(), project_id: null, type: "resync", payload: {} };
  dispatch(e);
  if (isLeader) channel?.postMessage({ kind: "event", event: e });
}

function openStream() {
  clearTimeout(reopenTimer);
  source?.close();
  const es = new EventSource(`/api/events?after=${lastId}`);
  source = es;
  es.onopen = () => {
    backoff = 1000;
    const reconnected = hadConnection;
    hadConnection = true;
    setConnection("live");
    if (reconnected) resync();
  };
  es.onmessage = (m) => {
    try { accept(JSON.parse(m.data), true); } catch { /* a malformed frame is skipped */ }
  };
  es.onerror = () => {
    if (source !== es) return;
    if (es.readyState === EventSource.CLOSED) {
      // The browser gave up (server restarted or answered an error): reopen ourselves, from where we stopped.
      es.close();
      setConnection("reconnecting");
      reopenTimer = setTimeout(openStream, backoff);
      backoff = Math.min(backoff * 1.7, 15000);
    } else {
      setConnection("reconnecting");   // EventSource retries by itself and sends Last-Event-ID
    }
  };
}

let pollTimer = null;
async function pollOnce() {
  try {
    const res = await get("/api/events/poll", { query: { after: lastId } });
    const wasDown = connection === "offline";
    for (const e of res.events || []) accept(e, true);
    if (typeof res.max_event_id === "number" && lastId === 0) lastId = res.max_event_id;
    setConnection("polling");
    if (wasDown) resync();
  } catch {
    setConnection("offline");
  }
}
function startPolling() {
  if (pollTimer) return;
  setConnection("polling");
  pollTimer = setInterval(pollOnce, POLL_MS);
}

function lead() {
  isLeader = true;
  if (typeof EventSource === "undefined") { startPolling(); return new Promise(() => {}); }
  openStream();
  return new Promise(() => {});   // hold the lock until this tab closes
}

/**
 * Start the stream after the snapshot's `max_event_id`. Safe to call once per page.
 * @param {number} afterId
 */
export function start(afterId) {
  if (started) return;
  started = true;
  lastId = afterId || 0;
  if (!TOKEN) { setConnection("offline"); return; }
  try { channel = new BroadcastChannel(CHANNEL); } catch { channel = null; }
  if (channel) {
    channel.onmessage = (m) => {
      const msg = m.data;
      if (msg?.kind === "event" && !isLeader) {
        if (msg.event.type === "resync") dispatch(msg.event);
        else accept(msg.event, false);
      } else if (msg?.kind === "status" && !isLeader) {
        setConnection(msg.status, false);
      } else if (msg?.kind === "hello" && isLeader) {
        channel?.postMessage({ kind: "status", status: connection });   // a tab that just opened asks how the stream is doing
      }
    };
    channel.postMessage({ kind: "hello" });
  }
  if (navigator.locks && channel) {
    // Whoever holds the lock owns the EventSource; when that tab closes the next waiting tab becomes the leader.
    navigator.locks.request(LOCK, { mode: "exclusive" }, () => lead()).catch(() => startPolling());
  } else {
    isLeader = true;
    startPolling();
  }
}

/** Test and diagnostics helper: what this tab is doing. */
export function describe() {
  return { leader: isLeader, connection, lastId, mode: pollTimer ? "poll" : source ? "sse" : "idle" };
}
