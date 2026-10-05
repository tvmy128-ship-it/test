// @ts-check
// Brush-mask editor for local edits (APP_SPEC 9.5, 12). The user paints over the picture where the change should happen.
// Output (POST /api/uploads/mask): an RGBA PNG exactly the size of the picture, painted pixels opaque white, everything
// else fully transparent. Painting needs a pointer, so the same mask can also be made with the "quick area" buttons
// (top, bottom, left, right, everything), which work with the keyboard.
import { h, uid } from "../dom.js";

/**
 * @param {{imageUrl: string, alt?: string, maxUndo?: number}} o
 */
export function createBrushMask(o) {
  const id = uid("bm");
  const img = h("img", { class: "mask-img", alt: o.alt ?? "", src: o.imageUrl });
  const canvas = h("canvas", { class: "mask-canvas", width: "512", height: "512", "aria-label": "Painted area. Use the buttons below if you cannot paint with a mouse or finger." });
  const stage = h("div", { class: "mask-stage" }, img, canvas);
  const ctx = /** @type {CanvasRenderingContext2D} */ (canvas.getContext("2d", { willReadFrequently: true }));
  let painting = false;
  let erasing = false;
  let size = 36;
  let dirty = false;
  /** @type {ImageData[]} */
  const undo = [];
  const maxUndo = o.maxUndo ?? 20;

  const syncSize = () => {
    if (img.naturalWidth && (canvas.width !== img.naturalWidth || canvas.height !== img.naturalHeight)) {
      canvas.width = img.naturalWidth;
      canvas.height = img.naturalHeight;
    }
  };
  img.addEventListener("load", syncSize);

  /** @param {PointerEvent} e */
  const pos = (e) => {
    const r = canvas.getBoundingClientRect();
    return { x: ((e.clientX - r.left) / r.width) * canvas.width, y: ((e.clientY - r.top) / r.height) * canvas.height };
  };
  const brushPx = () => (size / 512) * Math.max(canvas.width, canvas.height) * 0.5 + 4;
  const snapshot = () => {
    undo.push(ctx.getImageData(0, 0, canvas.width, canvas.height));
    if (undo.length > maxUndo) undo.shift();
  };
  /** @param {number} x @param {number} y */
  const dot = (x, y) => {
    ctx.globalCompositeOperation = erasing ? "destination-out" : "source-over";
    ctx.fillStyle = "#ff2d95";
    ctx.beginPath();
    ctx.arc(x, y, brushPx(), 0, Math.PI * 2);
    ctx.fill();
    dirty = true;
  };
  /** @type {{x: number, y: number} | null} */
  let last = null;
  canvas.addEventListener("pointerdown", (e) => {
    syncSize();
    painting = true;
    canvas.setPointerCapture(e.pointerId);
    snapshot();
    const p = pos(e);
    dot(p.x, p.y);
    last = p;
  });
  canvas.addEventListener("pointermove", (e) => {
    if (!painting || !last) return;
    const p = pos(e);
    const steps = Math.max(1, Math.ceil(Math.hypot(p.x - last.x, p.y - last.y) / (brushPx() / 2)));
    for (let i = 1; i <= steps; i += 1) dot(last.x + ((p.x - last.x) * i) / steps, last.y + ((p.y - last.y) * i) / steps);
    last = p;
  });
  const stop = () => { painting = false; last = null; };
  canvas.addEventListener("pointerup", stop);
  canvas.addEventListener("pointercancel", stop);

  /** @param {number} x0 @param {number} y0 @param {number} x1 @param {number} y1 fractions of the picture */
  const fillArea = (x0, y0, x1, y1) => {
    syncSize();
    snapshot();
    ctx.globalCompositeOperation = "source-over";
    ctx.fillStyle = "#ff2d95";
    ctx.fillRect(x0 * canvas.width, y0 * canvas.height, (x1 - x0) * canvas.width, (y1 - y0) * canvas.height);
    dirty = true;
  };
  const clear = () => { snapshot(); ctx.clearRect(0, 0, canvas.width, canvas.height); dirty = false; };
  const undoOnce = () => {
    const prev = undo.pop();
    if (!prev) return;
    ctx.putImageData(prev, 0, 0);
    dirty = ctx.getImageData(0, 0, canvas.width, canvas.height).data.some((v, i) => i % 4 === 3 && v > 0);
  };

  const sizeId = `${id}-size`;
  const sizeInput = h("input", { type: "range", id: sizeId, min: "8", max: "120", value: String(size), "aria-label": "Brush size" });
  sizeInput.addEventListener("input", () => { size = Number(sizeInput.value); });
  const eraseBtn = h("button", { type: "button", class: "btn small", "aria-pressed": "false", onclick: () => {
    erasing = !erasing;
    eraseBtn.setAttribute("aria-pressed", String(erasing));
    eraseBtn.textContent = erasing ? "Eraser is on" : "Eraser";
  } }, "Eraser");
  const area = (/** @type {string} */ label, /** @type {number[]} */ a) => h("button", { type: "button", class: "btn small", onclick: () => fillArea(a[0], a[1], a[2], a[3]) }, label);
  const tools = h("div", { class: "mask-tools" },
    h("label", { for: sizeId }, "Brush size"), sizeInput, eraseBtn,
    h("button", { type: "button", class: "btn small", onclick: undoOnce }, "Undo"),
    h("button", { type: "button", class: "btn small", onclick: clear }, "Clear"));
  const quick = h("div", { class: "mask-tools", role: "group", "aria-label": "Quick areas" },
    h("span", { class: "muted" }, "Quick areas:"),
    area("Top half", [0, 0, 1, 0.5]), area("Bottom half", [0, 0.5, 1, 1]), area("Left half", [0, 0, 0.5, 1]), area("Right half", [0.5, 0, 1, 1]), area("Everything", [0, 0, 1, 1]));
  const el = h("div", { class: "brush-mask" },
    h("p", { class: "hint" }, "Paint over the part that should change. Anything you leave unpainted is kept as it is."), stage, tools, quick);

  return {
    el,
    hasMask: () => dirty,
    clear,
    /** The mask as a PNG blob: painted = opaque white, unpainted = transparent. @returns {Promise<Blob>} */
    toBlob() {
      syncSize();
      const out = document.createElement("canvas");
      out.width = canvas.width;
      out.height = canvas.height;
      const octx = /** @type {CanvasRenderingContext2D} */ (out.getContext("2d"));
      const data = ctx.getImageData(0, 0, canvas.width, canvas.height);
      const px = data.data;
      for (let i = 0; i < px.length; i += 4) {
        const on = px[i + 3] > 40;
        px[i] = px[i + 1] = px[i + 2] = on ? 255 : 0;
        px[i + 3] = on ? 255 : 0;
      }
      octx.putImageData(data, 0, 0);
      return new Promise((resolve, reject) => out.toBlob((b) => (b ? resolve(b) : reject(new Error("could not make the mask"))), "image/png"));
    },
  };
}
