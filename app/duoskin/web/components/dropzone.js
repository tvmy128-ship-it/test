// @ts-check
// A file drop zone that also works with the keyboard: the whole box is a real button that opens the file picker,
// and files can be dropped on it. Checks the extension and size before anything is uploaded.
import { h, uid, fmtBytes } from "../dom.js";

/**
 * @typedef {object} DropzoneOptions
 * @property {string[]} accept extensions with the dot, lower case, e.g. [".glb", ".fbx"]
 * @property {number} [maxBytes]
 * @property {boolean} [multiple]
 * @property {string} [title] big text, e.g. "Drop your 3D file here"
 * @property {string} [hint] small text under the title
 * @property {(files: File[]) => void | Promise<void>} onFiles
 * @property {string} [label] accessible name of the button
 */

/**
 * @param {DropzoneOptions} o
 * @returns {HTMLElement & {setStatus(text: string, tone?: "info" | "ok" | "bad"): void, setBusy(on: boolean): void}}
 */
export function dropzone(o) {
  const id = uid("dz");
  const status = h("p", { class: "dz-status", id: `${id}-status`, role: "status" });
  const input = h("input", { type: "file", class: "sr", tabindex: "-1", "aria-hidden": "true", multiple: !!o.multiple, accept: o.accept.join(",") });
  const button = h("button", { type: "button", class: "dz-button", "aria-describedby": `${id}-status ${id}-hint` },
    h("span", { class: "dz-title" }, o.title ?? "Drop a file here, or press to choose one"),
    h("span", { class: "dz-hint", id: `${id}-hint` }, o.hint ?? `Accepted: ${o.accept.join(", ")}${o.maxBytes ? `, up to ${fmtBytes(o.maxBytes)}` : ""}`));
  if (o.label) button.setAttribute("aria-label", o.label);
  const box = h("div", { class: "dropzone" }, button, input, status);

  /** @param {File[]} files */
  const take = async (files) => {
    const bad = files.find((f) => !o.accept.some((ext) => f.name.toLowerCase().endsWith(ext)));
    if (bad) { setStatus(`"${bad.name}" is not a type we can open. Please use ${o.accept.join(" or ")}.`, "bad"); return; }
    const big = o.maxBytes ? files.find((f) => f.size > /** @type {number} */ (o.maxBytes)) : null;
    if (big) { setStatus(`"${big.name}" is ${fmtBytes(big.size)}, which is over the ${fmtBytes(/** @type {number} */ (o.maxBytes))} limit.`, "bad"); return; }
    if (!files.length) return;
    setStatus(files.length === 1 ? `Selected ${files[0].name}` : `Selected ${files.length} files`, "info");
    await o.onFiles(o.multiple ? files : files.slice(0, 1));
  };
  /** @param {string} text @param {"info" | "ok" | "bad"} [tone] */
  function setStatus(text, tone = "info") { status.textContent = text; status.className = `dz-status ${tone}`; }

  button.addEventListener("click", () => input.click());
  input.addEventListener("change", () => { const f = Array.from(input.files ?? []); input.value = ""; void take(f); });
  for (const ev of ["dragenter", "dragover"]) {
    box.addEventListener(ev, (e) => { e.preventDefault(); box.classList.add("over"); });
  }
  for (const ev of ["dragleave", "drop"]) {
    box.addEventListener(ev, (e) => { e.preventDefault(); box.classList.remove("over"); });
  }
  box.addEventListener("drop", (e) => {
    const dt = /** @type {DragEvent} */ (e).dataTransfer;
    if (dt?.files?.length) void take(Array.from(dt.files));
  });
  return Object.assign(box, {
    setStatus,
    /** @param {boolean} on */
    setBusy(on) { button.disabled = on; box.classList.toggle("busy", on); },
  });
}
