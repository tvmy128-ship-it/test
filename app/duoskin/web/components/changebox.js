// @ts-check
// The "Change…" box shown at every gate (APP_SPEC 9.6, 12): a text area, an optional "which one?" choice and optionally a
// brush mask for a local edit. The wording is fixed by the spec: describe the visible result; the app will show you the
// changes before anything runs.
import { h, uid } from "../dom.js";
import { post, friendly, ApiError } from "../api.js";
import { createBrushMask } from "./brushmask.js";

export const CHANGE_HINT = "Describe the visible result; the app will show you the changes before anything runs.";

/**
 * @typedef {object} ChangeBoxOptions
 * @property {string} [title]
 * @property {string} [placeholder] example text, e.g. "make her jacket teal"
 * @property {{value: string, label: string}[]} [targets] radio choices, e.g. A only / B only / both
 * @property {string} [defaultTarget]
 * @property {string} [targetLabel]
 * @property {string} [maskImageUrl] when given, the user can paint the area to change on this picture
 * @property {string} [submitLabel]
 * @property {(r: {text: string, target: string | null, maskSha: string | null}) => Promise<void> | void} onSubmit
 * @property {() => void} [onCancel]
 */

/** @param {ChangeBoxOptions} o */
export function changeBox(o) {
  const id = uid("chg");
  const area = h("textarea", { id: `${id}-text`, rows: "3", maxlength: "1000", placeholder: o.placeholder ?? "For example: make the jacket teal and the sleeves shorter", "aria-describedby": `${id}-hint` });
  const count = h("span", { class: "counter" }, "0 / 1000");
  area.addEventListener("input", () => { count.textContent = `${area.value.length} / 1000`; err.textContent = ""; });
  const err = h("p", { class: "note bad", role: "alert", id: `${id}-err` });
  /** @type {HTMLElement | null} */
  let targetGroup = null;
  if (o.targets?.length) {
    targetGroup = h("fieldset", { class: "choice-group" }, h("legend", {}, o.targetLabel ?? "Which one?"),
      o.targets.map((t, i) => {
        const rid = `${id}-t${i}`;
        return h("label", { class: "choice", for: rid }, h("input", { type: "radio", name: `${id}-target`, id: rid, value: t.value, checked: t.value === (o.defaultTarget ?? o.targets?.[0].value) }), h("span", {}, t.label));
      }));
  }
  const mask = o.maskImageUrl ? createBrushMask({ imageUrl: o.maskImageUrl, alt: "The picture to change" }) : null;
  const maskWrap = mask ? h("details", { class: "mask-details" }, h("summary", {}, "Only change one area (optional)"), mask.el) : null;
  const submit = h("button", { type: "button", class: "btn primary" }, o.submitLabel ?? "Show me the changes");
  const cancel = o.onCancel ? h("button", { type: "button", class: "btn", onclick: () => o.onCancel?.() }, "Cancel") : null;
  submit.addEventListener("click", async () => {
    const text = area.value.trim();
    if (text.length < 3) { err.textContent = "Please say what you would like to be different."; area.focus(); return; }
    submit.disabled = true;
    submit.setAttribute("aria-busy", "true");
    try {
      let maskSha = null;
      if (mask?.hasMask()) {
        const blob = await mask.toBlob();
        const form = new FormData();
        form.append("file", blob, "mask.png");
        try {
          const up = await post("/api/uploads/mask", form);
          maskSha = up?.sha ?? null;
        } catch (e) {
          if (e instanceof ApiError && e.unavailable) err.textContent = "Painting an area is not available yet, so the change will apply to the whole picture.";
          else throw e;
        }
      }
      const picked = targetGroup?.querySelector("input:checked");
      await o.onSubmit({ text, target: picked ? /** @type {HTMLInputElement} */ (picked).value : null, maskSha });
    } catch (e) {
      err.textContent = friendly(e);
    } finally {
      submit.disabled = false;
      submit.removeAttribute("aria-busy");
    }
  });
  const el = h("div", { class: "change-box" },
    o.title ? h("h3", {}, o.title) : null,
    h("label", { for: `${id}-text` }, "What should be different?"),
    area,
    h("div", { class: "hint-row" }, h("p", { class: "hint", id: `${id}-hint` }, CHANGE_HINT), count),
    targetGroup, maskWrap, err,
    h("div", { class: "row" }, cancel, submit));
  return Object.assign(el, { focusText: () => area.focus() });
}
