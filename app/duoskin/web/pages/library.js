// @ts-check
// Library (#/library): kits and registries. What is available (and what happens without each kit), the hair styles,
// head base variants, body base, fabrics and fold sets, "Add kit", "Build head base" and the registries.
import { post, tryGet, friendly, ApiError, casImage } from "../api.js";
import { h, humanize, setChildren } from "../dom.js";
import { pageHeader, panel, badge, field, note, notAvailable, errorNote, emptyState } from "../components/ui.js";
import { renderReport } from "../components/report.js";
import { toast } from "../components/toast.js";

const KINDS = [["hair", "Hair style"], ["head_base", "Head base"], ["body_base", "Body base"], ["fabric", "Fabric tile"], ["fold", "Fold set"], ["shoes", "Shoes"], ["recipe", "Clothing recipe"]];
const ORIGINS = [["own", "I made it"], ["purchased", "I bought it"], ["free_licensed", "Free, with a licence that allows this"], ["code_generated", "Made by the program"]];
const LICENCES = [["own_work", "My own work"], ["commercial_ok", "Licence allows commercial use"], ["cc0", "CC0 (public domain)"], ["cc_by", "CC BY (credit the author)"]];

/** @param {import("../router.js").PageContext} ctx */
export async function render(ctx) {
  const body = h("div", {});
  setChildren(ctx.root, pageHeader({ title: "Library", lead: "The kits and collections DuoSkin builds with: hair styles, head bases, fabrics, and what you have collected." }), body);

  const draw = async () => {
    /** @type {{data: any, unavailable: boolean}} */ let lib;
    try { lib = await tryGet("/api/library", { signal: ctx.signal }); } catch (err) { if (ctx.active()) setChildren(body, errorNote(friendly(err))); return; }
    if (!ctx.active()) return;
    if (lib.unavailable) { setChildren(body, notAvailable("The library"), h("p", { class: "muted" }, "Without kits, DuoSkin uses the built-in fallbacks: plain head, custom hair, procedural folds. Everything still works.")); return; }
    const d = lib.data || {};
    const avail = d.availability ?? d.flags ?? {};
    const labelled = Object.entries(avail).filter(([, v]) => typeof v === "boolean");
    const hair = /** @type {any[]} */ ((d.hair_styles ?? d.hair ?? []).map((/** @type {any} */ x) => (typeof x === "string" ? { id: x } : x)));
    const labelNotes = Object.values(/** @type {Record<string, string>} */ (d.labels ?? {}));
    const rest = Object.fromEntries(Object.entries(d).filter(([k]) => !["availability", "flags", "hair_styles", "hair", "labels", "manifest_sha", "demo", "hair_modules"].includes(k)));

    // add a kit
    const folder = h("input", { type: "text", class: "wide", placeholder: "C:\\path\\to\\the\\kit folder", spellcheck: "false" });
    const kind = h("select", {}, KINDS.map(([v, t]) => h("option", { value: v }, t)));
    const origin = h("select", {}, h("option", { value: "" }, "Choose…"), ORIGINS.map(([v, t]) => h("option", { value: v }, t)));
    const lic = h("select", {}, h("option", { value: "" }, "Choose…"), LICENCES.map(([v, t]) => h("option", { value: v }, t)));
    const addErr = h("div", { class: "msg" });
    const add = h("button", { type: "button", class: "btn primary", onclick: async () => {
      setChildren(addErr);
      if (!folder.value.trim() || !origin.value || !lic.value) { addErr.append(note("Fill in the folder, where it came from, and its licence. Both are required so nothing unlicensed gets in.", "bad")); return; }
      add.disabled = true;
      try { await post("/api/library/kits", { folder_path: folder.value.trim(), kind: kind.value, origin: origin.value, license: lic.value }); toast("Kit added. The manifest was rebuilt.", { kind: "ok" }); await draw(); } catch (e) { setChildren(addErr, e instanceof ApiError && e.unavailable ? note("Adding kits is not available yet in this build.", "warn") : note(friendly(e), "bad")); add.disabled = false; }
    } }, "Add this kit");
    // head base
    const src = h("input", { type: "text", class: "wide", placeholder: "C:\\path\\to\\head source", spellcheck: "false" });
    const variant = h("input", { type: "text", placeholder: "variant name", value: "default" });
    const headErr = h("div", { class: "msg" });
    const buildHead = h("button", { type: "button", class: "btn", onclick: async () => {
      setChildren(headErr);
      try { await post("/api/library/head-base/build", { source_path: src.value.trim(), variant: variant.value.trim() || "default" }); toast("Building the head base. Watch it on the Jobs page.", { kind: "ok" }); } catch (e) { setChildren(headErr, e instanceof ApiError && e.unavailable ? note("Building a head base is not available yet in this build.", "warn") : note(friendly(e), "bad")); }
    } }, "Build head base");
    const rebuild = h("button", { type: "button", class: "btn", onclick: async () => {
      try { const r = await post("/api/library/rebuild-manifest"); toast(r?.ok === false ? "The rebuilt manifest did not pass its smoke test." : "The manifest was rebuilt.", { kind: r?.ok === false ? "warn" : "ok" }); await draw(); } catch (e) { toast(e instanceof ApiError && e.unavailable ? "Not available yet in this build." : friendly(e), { kind: "warn" }); }
    } }, "Rebuild the manifest");

    setChildren(body, 
      labelled.length ? panel({ title: "What is available", lead: "Nothing here is required. Each missing kit has a fallback." },
        h("ul", { class: "plain-list kit-list" }, labelled.map(([k, v]) => h("li", { class: "kit-row" }, h("strong", {}, humanize(k.replace(/_present$|_empty$/, ""))), " ", badge(/_empty$/.test(k) ? (v ? "Empty" : "Ready") : v ? "Ready" : "Not added", /_empty$/.test(k) ? (v ? "muted" : "ok") : v ? "ok" : "muted")))),
        labelNotes.length ? h("p", { class: "muted small" }, "Right now: ", labelNotes.join(" · ")) : null) : null,
      hair.length ? panel({ title: "Hair styles" }, h("div", { class: "hair-grid" }, hair.map((hs) => h("figure", { class: "fig small" }, hs.silhouette_sha ? casImage(hs.silhouette_sha, { alt: `${hs.id || hs.name} silhouette` }) : h("div", { class: "fig-empty" }), h("figcaption", {}, humanize(String(hs.id || hs.name || "")))))),
        d.pair_iou ? h("details", {}, h("summary", {}, "How alike the styles are (pair overlap)"), renderReport(d.pair_iou)) : null) : null,
      Object.keys(rest).length ? panel({ title: "Collections" }, renderReport(rest)) : emptyState("The library is empty", "Add a kit below, or just start a duo: the built-in fallbacks are used."),
      h("div", { class: "grid-2" },
        panel({ title: "Add a kit", lead: "Where it came from and its licence are required." }, field("Folder", folder), h("div", { class: "grid-2" }, field("What kind", kind), field("Where it came from", origin)), field("Licence", lic), addErr, add),
        panel({ title: "Head base", lead: "Needs Blender. Without a head base, faces are shown as flat pictures." }, field("Source folder", src), field("Variant", variant), headErr, h("div", { class: "row" }, buildHead, rebuild))));
  };
  await draw();
  ctx.live(["job.state"], () => { void draw(); }, 1000);
}
