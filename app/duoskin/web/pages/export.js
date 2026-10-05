// @ts-check
// Export (#/p/<id>/export; APP_SPEC 10.13): the kit's file tree, "Open folder", a checklist per item with tick boxes (the server
// says which steps are locked: an upload line stays locked until its Studio test is ticked), banners and where each item came from.
//
// GET /api/exports/<id> (api/exports.py): {status: none|running|blocked|failed|built|done, reason, kit_dir, zip, banners: [text],
// mock, checks, manifest: {items: [...], files: [{path, bytes}]}, checklist: {items: [{item_id, character, type, channel_text,
// fee_robux, fee_note, requirements, notes, steps: [{step_id, text, kind, requires, ticked, locked}]}]}, version}.
// PATCH /api/exports/<id>/checklist {item_id, step_id, ticked, expected_version} -> {checklist, version}.
import { get, tryGet, post, patch, friendly, ApiError } from "../api.js";
import { h, humanize, fmtBytes, plural, setChildren } from "../dom.js";
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

/** What stopped the kit, in plain words (the gate's check ids and part ids stay in the log). @param {any} data @param {string} status */
function blockedText(data, status) {
  const checks = /** @type {string[]} */ (data.checks ?? []);
  if (data.mock || /mock source|DEMO/i.test(String(data.reason ?? ""))) return "The final checks stopped the kit, because this duo was made with practice (demo) services. Nothing here can be exported.";
  const WORDS = /** @type {Record<string, string>} */ ({
    "CHK-E01": "Some parts are missing or not finished, so there is nothing complete to hand over yet.",
    "CHK-E02": "A part changed after you approved it. Open the part board and approve it again.",
    "CHK-E03": "The record of how each part was made is incomplete.",
    "CHK-E04": "A file in the kit did not pass its final check.",
    "CHK-E05": "An item is set to go on the wrong place on the body.",
    "CHK-E06": "A key or password was found in a file that would be exported, so the kit was stopped to keep it private.",
    "CHK-E09": "A part uses material whose rights are not known, so it needs a person to confirm it.",
  });
  const said = checks.map((c) => WORDS[c]).filter(Boolean);
  if (said.length) return said.join(" ");
  return status === "blocked" ? "One of the final checks did not pass, so the kit was stopped." : "Something went wrong while packing the kit.";
}

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
    const version = Number(data.version ?? 0);
    /** @param {any} it @param {boolean} [preview] a preview lists the steps but nothing can be ticked: no kit was written */
    const itemCard = (it, preview = false) => h("section", { class: "check-item", dataset: { itemId: it.item_id } },
      h("h3", {}, it.character ? h("span", { class: `char-chip ${it.character}` }, String(it.character).toUpperCase()) : null, humanize(it.type)),
      h("p", { class: "muted" }, it.channel_text ?? "", it.fee_robux ? ` Upload fee: ${it.fee_robux} Robux${it.fee_note ? ` (${it.fee_note})` : ""}. Check Roblox for current prices.` : ""),
      it.requirements?.length ? h("ul", { class: "plain-list small muted" }, it.requirements.map((/** @type {string} */ r) => h("li", {}, r))) : null,
      h("ul", { class: "checklist" }, (it.steps ?? []).map((/** @type {any} */ s) => {
        const cid = `ck-${it.item_id}-${s.step_id}`.replace(/[^A-Za-z0-9_-]/g, "_");
        const cb = h("input", { type: "checkbox", id: cid, checked: Boolean(s.ticked), disabled: preview || Boolean(s.locked) });
        cb.addEventListener("change", async () => {
          try { await patch(`/api/exports/${encodeURIComponent(id)}/checklist`, { item_id: it.item_id, step_id: s.step_id, ticked: cb.checked, expected_version: version }); await draw(); } catch (e) { cb.checked = !cb.checked; toast(friendly(e), { kind: "bad" }); await draw(); }
        });
        return h("li", { class: s.locked ? "locked" : "" }, cb, h("label", { for: cid }, String(s.text || humanize(s.step_id))), s.locked && !preview ? h("span", { class: "muted" }, " (locked until the step before it is ticked)") : null);
      })),
      it.notes?.length ? h("ul", { class: "plain-list small muted" }, it.notes.map((/** @type {string} */ n) => h("li", {}, n))) : null);

    if (status === "none" || (!data.manifest && !data.checklist && !["running", "blocked", "failed"].includes(status))) {
      setChildren(body, demo ? note("Demo mode: nothing can be exported.", "warn") : null, ...bannerNotes, emptyState("The upload kit has not been made yet", "Pick the final duo first, then make the kit.", startBtn("Make the upload kit")));
      return;
    }
    if (status === "running") {
      setChildren(body, panel({ title: "Packing the upload kit" }, h("p", {}, "This usually takes a minute. You can leave this page."), progress(0.5, "Packing")));
      return;
    }
    if (status === "blocked" || status === "failed") {
      const preview = demo && data.preview?.checklist?.items?.length ? data.preview : null;      // only demo mode ends with a preview; in normal mode the block is the message
      setChildren(body, ...bannerNotes, panel({ class: "panel-error", title: preview ? "Export preview: nothing was written" : status === "blocked" ? "The kit is not ready to upload" : "The kit could not be made" },
        h("p", {}, blockedText(data, status)),
        data.mock ? h("p", { class: "muted" }, demo ? "Demo mode is on, so this is the end of the road for a practice duo: you can look at everything, but it can never be uploaded. To make a duo you can upload, turn demo mode off, add your keys in Setup and start a new duo."
          : "This duo was made with practice stand-ins, so it can be looked at but never uploaded. Use your real keys (Setup) and start a new duo to make one you can upload.") : null,
        project?.stage === "gate3" && !data.mock ? startBtn("Try again") : null,
        h("p", {}, h("a", { class: "btn", href: `#/p/${id}/gate3` }, "Back to the final pick"), " ", h("a", { class: "btn", href: `#/p/${id}` }, "Back to the duo"))),
      preview ? panel({ title: "What the kit would contain", lead: "This is the checklist you would follow with a real duo. It is a preview: nothing can be ticked or uploaded." },
        h("p", { class: "muted" }, plural(preview.items.length, "item"), ": ", [...new Set(/** @type {any[]} */ (preview.items).map((i) => `${String(i.character).toUpperCase()} ${humanize(i.type)}`))].join(", ")),
        h("div", { class: "check-items" }, /** @type {any[]} */ (preview.checklist.items).map((it) => itemCard(it, true)))) : null);
      return;
    }
    const files = filesOf(data);
    const items = /** @type {any[]} */ (data.manifest?.items ?? []);
    const checklist = /** @type {any[]} */ (data.checklist?.items ?? []);
    const openFolder = h("button", { type: "button", class: "btn", onclick: async () => {
      try { await post("/api/os/open-folder", { kind: "export", id }); toast("Opened the folder in Explorer.", { kind: "ok" }); } catch (e) { toast(e instanceof ApiError && e.unavailable ? "Opening folders is not available yet in this build." : friendly(e), { kind: "warn" }); }
    } }, "Open the folder");

    setChildren(body,
      demo || data.mock ? note("Demo mode: practice pictures were used, so this kit can be looked at but not uploaded.", "warn") : null,
      ...bannerNotes,
      panel({ title: "What is in the kit", actions: openFolder }, files.length ? tree(files) : h("p", { class: "muted" }, "The file list is empty so far."),
        data.kit_dir ? h("p", { class: "muted" }, "Folder: ", h("code", {}, String(data.kit_dir))) : null, data.zip ? h("p", { class: "muted" }, "Zip: ", h("code", {}, String(data.zip))) : null),
      panel({ title: "Before you upload", lead: "Tick each step as you do it. A step stays locked until the one it depends on is ticked: the upload lines wait for the test in Roblox Studio." },
        checklist.length ? h("div", { class: "check-items" }, checklist.map((it) => itemCard(it))) : h("p", { class: "muted" }, "The checklist appears once the kit is made."),
        data.checklist?.notes?.length ? h("ul", { class: "plain-list small muted" }, data.checklist.notes.map((/** @type {string} */ n) => h("li", {}, n))) : null),
      items.length ? panel({ title: "Where everything came from" }, h("div", { class: "table-wrap" }, h("table", { class: "table" }, h("thead", {}, h("tr", {}, ["Item", "Licence", "Made from", "Triangles", "FBX"].map((t) => h("th", {}, t)))),
        h("tbody", {}, items.map((p) => h("tr", {}, h("td", {}, `${String(p.character || "").toUpperCase()} ${humanize(p.type || "")}`.trim()), h("td", {}, licenceLabel(p.license || "n/a")),
          h("td", {}, (p.lineage ?? []).length ? (p.lineage ?? []).map((/** @type {any} */ l) => humanize(String(l.source || l.origin || ""))).filter((x, i, a) => x && a.indexOf(x) === i).join(", ") : "—"),
          h("td", {}, p.tris != null ? Number(p.tris).toLocaleString() : "—"), h("td", {}, p.fbx ? humanize(String(p.fbx)) : "—"))))))) : null);
  };
  await draw();
  ctx.live(["job.state", "project.stage", "step.state"], () => { void draw(); }, 600);
}
