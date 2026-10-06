// @ts-check
// API keys (APP_SPEC 14.2): one row per provider with its status and a "Test key" button. The page only ever sees the
// masked tail the server returns ("sk-…abcd"); the typed value is sent once and the box is emptied at once.
import { get, put, post, del, friendly } from "../api.js";
import { h, uid, setChildren } from "../dom.js";
import { badge } from "./ui.js";
import { confirmDialog } from "./modal.js";
import { toast } from "./toast.js";
import { providerLabel } from "../text.js";

const REQUIREMENT = /** @type {Record<string, [string, string]>} */ ({ required: ["Needed", "bad"], recommended: ["Recommended", "warn"], optional: ["Optional", "muted"] });
const WHY = /** @type {Record<string, string>} */ ({
  anthropic: "Writes the plans and judges the results.", openai: "Draws the concept pictures and parts.", tripo: "Makes the 3D models for hair and accessories.",
  recraft: "Draws faces and flat prints.", gemini: "An optional second opinion.", fal: "Optional extras.",
});

/** @param {any} k */
function statusText(k) {
  if (!k.set) return "Not set";
  const where = k.source === "environment" ? "from your computer's settings" : "saved on this computer";
  return `Set (${k.masked || "hidden"}), ${where}`;
}

/**
 * @param {{onChange?: () => void}} [o]
 * @returns {Promise<HTMLElement>}
 */
export async function keysTable(o = {}) {
  const host = h("div", { class: "keys" });
  const draw = async () => {
    /** @type {Record<string, any>} */ let keys;
    try { keys = await get("/api/keys"); } catch (err) { setChildren(host, h("p", { class: "note bad", role: "alert" }, friendly(err))); return; }
    setChildren(host, h("p", { class: "muted" }, "Keys are stored in Windows Credential Manager. They are never shown again after you save them."),
      h("div", { class: "key-list" }, Object.values(keys).map((k) => {
        const id = uid("key");
        const input = h("input", { type: "password", id, autocomplete: "new-password", spellcheck: "false", placeholder: k.set ? "Paste a new key to replace it" : "Paste your key here", "aria-describedby": `${id}-status`, name: `key-${k.provider}` });
        const result = h("p", { class: "key-result small", role: "status" });
        const [reqLabel, reqTone] = REQUIREMENT[k.requirement] ?? ["Optional", "muted"];
        if (k.last_test) { result.textContent = `Last test: ${k.last_test.ok ? "worked" : k.last_test.ok === false ? "did not work" : "not run"}${k.last_test.message ? ". " + k.last_test.message : ""}`; result.className = `key-result small ${k.last_test.ok ? "ok" : k.last_test.ok === false ? "bad" : "muted"}`; }
        const save = h("button", { type: "button", class: "btn", onclick: async () => {
          const value = input.value.trim();
          if (!value) { result.textContent = "Paste the key first."; result.className = "key-result small bad"; input.focus(); return; }
          save.disabled = true;
          try { await put(`/api/keys/${k.provider}`, { value }); input.value = ""; toast(`${providerLabel(k.provider)} key saved.`, { kind: "ok" }); o.onChange?.(); await draw(); } catch (e) { result.textContent = friendly(e); result.className = "key-result small bad"; save.disabled = false; }
        } }, "Save key");
        const test = h("button", { type: "button", class: "btn", onclick: async () => {
          if (k.test_cost_note && !(await confirmDialog({ title: `Test the ${providerLabel(k.provider)} key?`, message: k.test_cost_note, confirmLabel: "Run the test" }))) return;
          test.disabled = true;
          result.textContent = "Testing…";
          result.className = "key-result small muted";
          try {
            const r = await post(`/api/keys/${k.provider}/test`);
            result.textContent = r.ok === true ? `It works. ${r.message || ""}` : r.ok === false ? `It did not work. ${r.message || ""}` : (r.message || "This key cannot be tested yet.");
            result.className = `key-result small ${r.ok === true ? "ok" : r.ok === false ? "bad" : "muted"}`;
            o.onChange?.();
          } catch (e) { result.textContent = friendly(e); result.className = "key-result small bad"; } finally { test.disabled = false; }
        } }, "Test key");
        const remove = k.set && k.source !== "environment" ? h("button", { type: "button", class: "btn quiet", onclick: async () => {
          if (!(await confirmDialog({ title: "Remove this key?", message: "You can paste it again later.", confirmLabel: "Remove", tone: "danger" }))) return;
          try { await del(`/api/keys/${k.provider}`); o.onChange?.(); await draw(); } catch (e) { toast(friendly(e), { kind: "bad" }); }
        } }, "Remove") : null;
        return h("div", { class: "key-row", dataset: { provider: k.provider } },
          h("div", { class: "key-head" }, h("label", { for: id }, h("strong", {}, providerLabel(k.provider))), badge(reqLabel, /** @type {any} */ (reqTone)),
            k.mode && k.mode !== "real" ? badge(k.mode === "mock" ? "Practice mode" : "Switched off", "info") : null, k.paused ? badge("Paused", "warn") : null),
          h("p", { class: "muted small" }, WHY[k.provider] ?? ""),
          h("p", { class: `key-status ${k.set ? "ok" : ""}`, id: `${id}-status` }, statusText(k)),
          h("div", { class: "row" }, input, save, test, remove),
          k.test_cost_note ? h("p", { class: "muted small" }, k.test_cost_note) : null, result);
      })));
  };
  await draw();
  return host;
}
