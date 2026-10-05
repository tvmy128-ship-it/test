// @ts-check
// What the buttons on a tile do (APP_SPEC 9.5): one function for the tile and its drawer, so both behave the same way.
import { get, post, friendly, casUrl, extForRole, ApiError } from "../api.js";
import { h, remembered, remember } from "../dom.js";
import { decide } from "./decisions.js";
import { changeBox } from "./changebox.js";
import { followChange } from "./changeflow.js";
import { confirmDialog, openDialog } from "./modal.js";
import { toast } from "./toast.js";
import { partKindOf } from "./tile.js";

export const FACE_TARGETS = [
  { value: "all", label: "The whole face" },
  { value: "iris", label: "Just the eyes (iris)" },
  { value: "lash", label: "Just the lashes" },
  { value: "brow", label: "Just the eyebrows" },
  { value: "mouth_closed", label: "Just the closed mouth" },
  { value: "mouth_open", label: "Just the open mouth" },
];

/**
 * @typedef {object} ActionEnv
 * @property {string} projectId
 * @property {{id: string}} gate
 * @property {any} tile
 * @property {() => Promise<void> | void} refresh reload the gate after a decision
 * @property {Record<string, string>} [partLabels]
 * @property {(path: string) => void} [navigate]
 */

/** The hero picture of a tile, for the brush-mask editor. @param {any} tile */
function heroUrl(tile) {
  const assets = /** @type {Record<string, string>} */ (tile.assets || {});
  const roles = Object.keys(assets);
  const role = ["front", "view_front", "flat_front", "graphic", "final"].find((r) => roles.includes(r)) ?? roles[0];
  return role ? casUrl(assets[role], extForRole(role)) : "";
}

/** @param {string} label @param {{value: string, label: string}[]} targets @param {string} verb */
function pickTarget(label, targets, verb) {
  /** @type {import("./modal.js").DialogHandle} */
  let dlg;
  const group = h("fieldset", { class: "choice-group" }, h("legend", {}, "Which part?"),
    targets.map((t, i) => h("label", { class: "choice" }, h("input", { type: "radio", name: "tt", value: t.value, checked: i === 0 }), h("span", {}, t.label))));
  const go = h("button", { type: "button", class: "btn primary", onclick: () => dlg.close(/** @type {HTMLInputElement | null} */ (group.querySelector("input:checked"))?.value ?? targets[0].value) }, verb);
  dlg = openDialog({ title: label, body: group, actions: [h("button", { type: "button", class: "btn", onclick: () => dlg.close(undefined) }, "Cancel"), go] });
  go.focus();
  return /** @type {Promise<string | undefined>} */ (dlg.closed);
}

/**
 * @param {ActionEnv} env
 * @param {string} action
 * @param {{choice?: string, target?: string, closeDrawer?: () => void}} [extra]
 */
export async function runTileAction(env, action, extra = {}) {
  const { gate, tile } = env;
  const kind = partKindOf(tile);
  const what = tile.label ? `“${tile.label}”` : "";
  switch (action) {
    case "approve": {
      const r = await decide(gate, tile, "approve", { what });
      if (r.status === "done") toast(`${tile.label || "Part"} approved.`, { kind: "ok" });
      await env.refresh();
      if (r.released?.length && r.status === "done") toast("Some tiles now show a heads-up. You can still go ahead.", { kind: "info" });
      return r;
    }
    case "reimagine": {
      /** @type {string | undefined} */
      let target = extra.target;
      if (kind === "face" && !target) {
        target = await pickTarget("Reimagine which part of the face?", FACE_TARGETS, "Reimagine");
        if (!target) return null;
      }
      const ok = await confirmDialog({ title: "Draw this again?", message: "A fresh version is made with a new idea. This can cost a little; the cost bar shows what it adds. The version you have now is kept as an alternative.", confirmLabel: "Reimagine", cancelLabel: "Not now" });
      if (!ok) return null;
      const r = await decide(gate, tile, "reimagine", { target });
      if (r.status === "done") toast("Making a new version. It will appear here when it is ready.", { kind: "info" });
      await env.refresh();
      return r;
    }
    case "change": {
      const targets = kind === "face" ? FACE_TARGETS : undefined;
      /** @type {import("./modal.js").DialogHandle} */
      let dlg;
      const box = changeBox({
        title: `Change ${tile.label || "this part"}`,
        targets, defaultTarget: extra.target ?? "all", targetLabel: "What should change?",
        maskImageUrl: heroUrl(tile) || undefined,
        onSubmit: async ({ text, target, maskSha }) => {
          const before = await openGateIds(env.projectId);
          const r = await decide(gate, tile, "change", { text, target: target ?? extra.target ?? null, maskSha });
          if (r.status === "error") throw new Error(r.message);
          dlg.close(true);
          extra.closeDrawer?.();
          await env.refresh();
          if (r.status === "done") await followChange({ projectId: env.projectId, changeId: r.decision?.change_request_id ?? null, afterGateIds: before, partLabels: env.partLabels, onApplied: () => { void env.refresh(); } });
          await env.refresh();
        },
        onCancel: () => dlg.close(false),
      });
      dlg = openDialog({ title: "Change…", wide: true, body: box });
      box.focusText();
      return dlg.closed;
    }
    case "make_manual": {
      if (!remembered("duoskin-free-plan-seen")) {
        const ok = await confirmDialog({ title: "Before you use Tripo's website", confirmLabel: "I understand, make the pack", message: h("div", {}, h("p", {}, "On Tripo's FREE plan, your model becomes public (labelled CC BY 4.0) and you get no commercial-use rights."), h("p", {}, "Use a paid plan for anything you may sell, and do not upload unreleased designs on the free plan.")) });
        if (!ok) return null;
        remember("duoskin-free-plan-seen", "1");
      }
      try {
        const res = await post(`/api/projects/${encodeURIComponent(env.projectId)}/parts/${encodeURIComponent(tile.part_id || tile.tile_id)}/tripo-pack`);
        toast(res?.pack_id ? `Your Tripo pack ${res.pack_id} is ready.` : "Your Tripo pack is ready.", { kind: "ok" });
        env.navigate?.(`/p/${env.projectId}/build`);
      } catch (err) {
        toast(err instanceof ApiError && err.unavailable ? "Making a Tripo pack is not available yet in this build." : friendly(err), { kind: err instanceof ApiError && err.unavailable ? "warn" : "bad" });
      }
      return null;
    }
    case "flip_mirrored": {
      const ok = await confirmDialog({ title: "Flip left and right?", message: "Only do this if you checked the 3D preview and the model really is mirrored. DuoSkin never flips it by itself.", confirmLabel: "Flip it" });
      if (!ok) return null;
      const r = await decide(gate, tile, "flip_mirrored");
      await env.refresh();
      return r;
    }
    case "select_alternative": {
      const r = await decide(gate, tile, "select_alternative", { choice: extra.choice });
      if (r.status === "done") toast("Switched to that version.", { kind: "ok" });
      await env.refresh();
      return r;
    }
    default:
      return null;
  }
}

/** @param {string} projectId */
async function openGateIds(projectId) {
  try {
    const gates = await get("/api/gates", { query: { project_id: projectId, state: "open" } });
    return /** @type {{id: string}[]} */ (gates).map((g) => g.id);
  } catch { return []; }
}
