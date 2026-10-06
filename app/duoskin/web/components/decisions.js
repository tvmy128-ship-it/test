// @ts-check
// Sending a decision to a gate (APP_SPEC 9.1, 9.5, 9.9): idempotent by client_decision_id, optimistic lock by the tile's
// version, and the "Approve anyway?" confirm when the server holds the approval as provisional.
import { post, del, ApiError, friendly } from "../api.js";
import { randomId } from "../dom.js";
import { approveAnywayDialog } from "./warnings.js";
import { toast } from "./toast.js";

/**
 * @typedef {object} DecisionExtra
 * @property {string} [text] the "Change…" text or New-plan reasons
 * @property {string | null} [maskSha] a brush mask uploaded earlier
 * @property {string | null} [choice] alternative index, plan id, candidate id or new cap
 * @property {string | null} [target] "a" | "b" | "both", or a face part
 * @property {string} [what] short words for the approve-anyway dialog ("the face")
 */

/**
 * @typedef {object} DecisionResult
 * @property {"done" | "back" | "conflict" | "error"} status
 * @property {any} [decision]
 * @property {any[]} [released] warnings released by this decision
 * @property {string} [message] plain-English error
 */

/**
 * @param {{id: string}} gate
 * @param {{tile_id: string, version: number}} tile
 * @param {string} action a GateAction value: "approve", "reimagine", "change", ...
 * @param {DecisionExtra} [extra]
 * @returns {Promise<DecisionResult>}
 */
export async function decide(gate, tile, action, extra = {}) {
  /** @type {Record<string, any>} */
  const body = { tile_id: tile.tile_id, action, expected_version: tile.version, client_decision_id: randomId() };
  if (extra.text) body.text = extra.text;
  if (extra.maskSha) body.mask_sha = extra.maskSha;
  if (extra.choice != null) body.choice = String(extra.choice);
  if (extra.target) body.target = extra.target;
  let out;
  try {
    out = await post(`/api/gates/${encodeURIComponent(gate.id)}/decisions`, body);
  } catch (err) {
    if (err instanceof ApiError && err.conflict && ["version_conflict", "gate_closed"].includes(err.code)) {
      toast(err.message, { kind: "warn" });
      return { status: "conflict", message: err.message };
    }
    const message = friendly(err);
    toast(message, { kind: "bad" });
    return { status: "error", message };
  }
  const released = /** @type {any[]} */ (out.released_warnings || []);
  if (out.provisional && released.length) {
    const approve = await approveAnywayDialog(released, { what: extra.what });
    try {
      if (approve) {
        const confirmed = await post(`/api/gates/${encodeURIComponent(gate.id)}/decisions/${encodeURIComponent(out.decision.id)}/confirm`,
          { override_warnings: released.map((/** @type {{id: string}} */ w) => w.id) });
        return { status: "done", decision: confirmed, released };
      }
      await del(`/api/gates/${encodeURIComponent(gate.id)}/decisions/${encodeURIComponent(out.decision.id)}`);
      return { status: "back", released };
    } catch (err) {
      const message = friendly(err);
      toast(message, { kind: "bad" });
      return { status: "error", message };
    }
  }
  return { status: "done", decision: out.decision, released };
}
