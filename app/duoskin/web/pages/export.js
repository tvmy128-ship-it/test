// @ts-check
// Export (#/p/<id>/export; APP_SPEC 10.13): the kit's file tree, "Open folder", a checklist per item with tick boxes (the server
// says which steps are locked: an upload line stays locked until its Studio test is ticked), banners and where each item came from.
//
// GET /api/exports/<id> (api/exports.py): {status: none|running|blocked|failed|built|done, reason, kit_dir, zip, banners: [text],
// mock, checks, manifest: {items: [...], files: [{path, bytes}]}, checklist: {items: [{item_id, character, type, channel_text,
// fee_robux, fee_note, requirements, notes, steps: [{step_id, text, kind, requires, ticked, locked}]}]}, version}.
// PATCH /api/exports/<id>/checklist {item_id, step_id, ticked, expected_version} -> {checklist, version}.
import { get, tryGet, post, patch, friendly, ApiError } from "../api.js";
import { h, humanize, fmtBytes, setChildren } from "../dom.js";
import { pageHeader, panel, note, emptyState, errorNote, notAvailable, progress } from "../components/ui.js";
import { toast } from "../components/toast.js";
import { licenceLabel } from "../text.js";

/** @param {{path: string, size?: number, bytes?: number}[]} files */
function tree(files) {
  /** @type {any} */ const root = {};
  for (const f of files) {
    const segs = String(f.path).split(/[\\/]/).filter(Boolean);
    let node = root;
    segs.forEach((s, i) => { node.children ??= {}; node.children[s] ??= {}; node = node.children[s]; if (i === segs.length - 1) node.file = f; });
  }
  /** @param {any} n @param {number} depth */
  const draw = (n, depth) => h("ul", { class: "tree" }, Object.entries(n.children ?? {}).sort(([a, x], [b, y]) => (Boolean(/** @type {any} */ (x).children) === Boolean(/** @type {any} */ (y).children) ? a.localeCompare(b) : /** @type {any} */ (x).children ? -1 : 1)).map(([name, child]) => {
    const c = /** @type {any} */ (child);
    return c.children
      ? h("li", {}, h("details", { open: depth < 1 }, h("summary", {}, h("span", { class: "folder" }, name + "/")), draw(c, depth + 1)))
      : h("li", {}, h("span", { class: "file" }, name), c.file?.size ?? c.file?.bytes ? h("span", { class: "muted" }, ` ${fmtBytes(c.file.size ?? c.file.bytes)}`) : null);
  }));
  return draw(root, 0);
}

/** @param {any} data */
function filesOf(data) {
  const raw = data?.manifest?.files ?? data?.files ?? [];
  if (Array.isArray(raw)) return raw.map((/** @type {any} */ f) => (typeof f === "string" ? { path: f } : f));
  return Object.entries(raw).map(([path, v]) => ({ path, size: typeof v === "number" ? v : /** @type {any} */ (v)?.bytes }));
}

/** @param {any} b */
const bannerText = (b) => (typeof b === "string" ? b : String(b?.text || b?.message || "")).replace(/^([ab])\.(\w+):\s*/i, (_m, c, part) => `Character ${String(c).toUpperCase()}, ${humanize(part).toLowerCase()}: `);
/** @param {string} t */
const bannerTone = (t) => (/FBX not produced|off$|degraded|No Head|Standard Block/i.test(t) ? "info" : "warn");

/** @param {import("../router.js").PageContext} ctx */
export async function render(ctx) {
  const id = ctx.params.id;
  const body = h("div", {});
  setChildren(ctx.root, pageHeader({ title: "Export", lead: "Everything you need to upload to Roblox, in one folder, with a checklist so nothing is forgotten.", back: { href: `#/p/${id}`, label: "Your duo" } }), body);

  const draw = async () => {
    /** @type {any} */ let bundle; let ex = { data: /** @type {any} */ (null), unavailable: false };
    try {
      bundle = await get(`/api/projects/${encodeURIComponent(id)}`, { signal: ctx.signal });
      ex = await tryGet(`/api/exports/${encodeURIComponent(id)}`, { signal: ctx.signal });
    } catch (err) {
      if (ctx.active()) setChildren(body, errorNote(friendly(err)));
      return;
    }
    if (!ctx.active()) return;
    const project = bundle?.project;
    const demo = Boolean((await get("/api/health").catch(() => ({ demo: false }))).demo);
    if (ex.unavailable) {
      setChildren(body, demo ? note("Demo mode: nothing can be exported. This page shows what the finished kit looks like once real services are used.", "warn") : null, notAvailable("The export kit"), h("p", {}, h("a", { class: "btn", href: `#/p/${id}` }, "Back to the duo")));
      return;
    }
    const data = ex.data ?? {};
    const status = String(data.status ?? "none");
    const canStart = ["gate3", "exporting", "exported"].includes(project?.stage);
    /** @param {string} label */
    const startBtn = (label) => {
      const b = h("button", { type: "button", class: "btn primary big", disabled: !canStart, title: canStart ? "" : "Pick the final duo first", onclick: async () => {
        b.disabled = true;
        try { await post(`/api/projects/${id}/export`); toast("Packing the upload kit…", { kind: "ok" }); await draw(); } catch (e) { toast(friendly(e), { kind: "bad" }); b.disabled = false; }
      } }, label);
      return b;
    };
    const banners = /** @type {any[]} */ ([...(data.banners ?? []), ...(data.checklist?.banners ?? [])]).map(bannerText).filter((t, i, a) => t && a.indexOf(t) === i);
    const bannerNotes = banners.map((t) => note(t, /** @type {any} */ (bannerTone(t))));
    if (status === "none" || (!data.manifest && !data.checklist && !["running", "blocked", "failed"].includes(status))) {
      setChildren(body, demo ? note("Demo mode: nothing can be exported.", "warn") : null, ...bannerNotes, emptyState("The upload kit has not been made yet", "Pick the final duo first, then make the kit.", startBtn("Make the upload kit")));
      return;
    }
    if (status === "running") {
      setChildren(body, panel({ title: "Packing the upload kit" }, h("p", {}, "This usually takes a minute. You can leave this page."), progress(0.5, "Packing")));
      return;
    }
    if (status === "blocked" || status === "failed") {
      setChildren(body, ...bannerNotes, panel({ class: "panel-error", title: status === "blocked" ? "The kit is not ready to upload" : "The kit could not be made" },
        h("p", {}, data.reason ? String(data.reason).replace(/^[A-Z]{2,5}-[A-Z]?\d+[a-z]?:\s*/, "") : status === "blocked" ? "One of the final checks did not pass." : "Something went wrong while packing it."),
        data.mock ? h("p", { class: "muted" }, "This duo was made with practice (demo) services, so it can be looked at but never exported.") : null,
        project?.stage === "gate3" ? startBtn("Try again") : null));
      return;
    }
    const files = filesOf(data);
    const items = /** @type {any[]} */ (data.manifest?.items ?? []);
    const checklist = /** @type {any[]} */ (data.checklist?.items ?? []);
    const version = Number(data.version ?? 0);
    const openFolder = h("button", { type: "button", class: "btn", onclick: async () => {
      try { await post("/api/os/open-folder", { kind: "export", id }); toast("Opened the folder in Explorer.", { kind: "ok" }); } catch (e) { toast(e instanceof ApiError && e.unavailable ? "Opening folders is not available yet in this build." : friendly(e), { kind: "warn" }); }
    } }, "Open the folder");

    /** @param {any} it */
    const itemCard = (it) => h("section", { class: "check-item", dataset: { itemId: it.item_id } },
      h("h3", {}, it.character ? h("span", { class: `char-chip ${it.character}` }, String(it.character).toUpperCase()) : null, humanize(it.type)),
      h("p", { class: "muted" }, it.channel_text ?? "", it.fee_robux ? ` Upload fee: ${it.fee_robux} Robux${it.fee_note ? ` (${it.fee_note})` : ""}.` : ""),
      it.requirements?.length ? h("ul", { class: "plain-list small muted" }, it.requirements.map((/** @type {string} */ r) => h("li", {}, r))) : null,
      h("ul", { class: "checklist" }, (it.steps ?? []).map((/** @type {any} */ s) => {
        const cid = `ck-${it.item_id}-${s.step_id}`.replace(/[^A-Za-z0-9_-]/g, "_");
        const cb = h("input", { type: "checkbox", id: cid, checked: Boolean(s.ticked), disabled: Boolean(s.locked) });
        cb.addEventListener("change", async () => {
          try { await patch(`/api/exports/${encodeURIComponent(id)}/checklist`, { item_id: it.item_id, step_id: s.step_id, ticked: cb.checked, expected_version: version }); await draw(); } catch (e) { cb.checked = !cb.checked; toast(friendly(e), { kind: "bad" }); await draw(); }
        });
        return h("li", { class: s.locked ? "locked" : "" }, cb, h("label", { for: cid }, String(s.text || humanize(s.step_id))), s.locked ? h("span", { class: "muted" }, " (locked until the step before it is ticked)") : null);
      })),
      it.notes?.length ? h("ul", { class: "plain-list small muted" }, it.notes.map((/** @type {string} */ n) => h("li", {}, n))) : null);

    setChildren(body,
      demo || data.mock ? note("Demo mode: practice pictures were used, so this kit can be looked at but not uploaded.", "warn") : null,
      ...bannerNotes,
      panel({ title: "What is in the kit", actions: openFolder }, files.length ? tree(files) : h("p", { class: "muted" }, "The file list is empty so far."),
        data.kit_dir ? h("p", { class: "muted" }, "Folder: ", h("code", {}, String(data.kit_dir))) : null, data.zip ? h("p", { class: "muted" }, "Zip: ", h("code", {}, String(data.zip))) : null),
      panel({ title: "Before you upload", lead: "Tick each step as you do it. A step stays locked until the one it depends on is ticked: the upload lines wait for the test in Roblox Studio." },
        checklist.length ? h("div", { class: "check-items" }, checklist.map(itemCard)) : h("p", { class: "muted" }, "The checklist appears once the kit is made."),
        data.checklist?.notes?.length ? h("ul", { class: "plain-list small muted" }, data.checklist.notes.map((/** @type {string} */ n) => h("li", {}, n))) : null),
      items.length ? panel({ title: "Where everything came from" }, h("div", { class: "table-wrap" }, h("table", { class: "table" }, h("thead", {}, h("tr", {}, ["Item", "Licence", "Made from", "Triangles", "FBX"].map((t) => h("th", {}, t)))),
        h("tbody", {}, items.map((p) => h("tr", {}, h("td", {}, `${String(p.character || "").toUpperCase()} ${humanize(p.type || "")}`.trim()), h("td", {}, licenceLabel(p.license || "n/a")),
          h("td", {}, (p.lineage ?? []).length ? (p.lineage ?? []).map((/** @type {any} */ l) => humanize(String(l.source || l.origin || ""))).filter((x, i, a) => x && a.indexOf(x) === i).join(", ") : "—"),
          h("td", {}, p.tris != null ? Number(p.tris).toLocaleString() : "—"), h("td", {}, p.fbx ? humanize(String(p.fbx)) : "—"))))))) : null);
  };
  await draw();
  ctx.live(["job.state", "project.stage", "step.state"], () => { void draw(); }, 600);
}
