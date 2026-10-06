// @ts-check
// Build (#/p/<id>/build; APP_SPEC 10, 11): progress per part, the Tripo steps ("slow" is normal), and in manual mode the
// pack folder, the drop zone and the import wizard (which Tripo plan made the file, licence banner, 3D preview).
import { get, tryGet, post, friendly, ApiError } from "../api.js";
import { h, humanize, uid, fmtBytes, setChildren } from "../dom.js";
import { pageHeader, panel, badge, progress, emptyState, errorNote, notAvailable, note } from "../components/ui.js";
import { dropzone } from "../components/dropzone.js";
import { viewerPanel } from "../components/viewer3d.js";
import { openGatePanels } from "../components/gatepanels.js";
import { decide } from "../components/decisions.js";
import { confirmDialog } from "../components/modal.js";
import { toast } from "../components/toast.js";
import { state as store } from "../store.js";
import { stepLabel, stepStateLabel, partKindLabel, stateLabel, checkLabel } from "../text.js";

const ACCEPT = [".glb", ".gltf", ".fbx", ".zip", ".obj", ".blend"];
const FOLD_AFTER = 5;       // more finished steps than this in one part: the older ones fold away
const KEEP_VISIBLE = 2;     // ... except the latest few, which stay in view
const FREE_BANNER = "This file came from Tripo's FREE plan, so the model is public (CC BY 4.0) and carries no commercial rights. It can be used to try things out, but it can never be marked ready to sell.";

/** @param {any} s */
function stepNote(s) {
  // a finished step says nothing more: its message is the handler's own log line ("4 draft(s) by I5", "4/4 pass Gate A")
  if (["succeeded", "superseded", "cancelled"].includes(s.state)) return "";
  if (s.state === "waiting_user") return "Waiting for your 3D file.";
  if (s.remote_state === "slow") return "Taking a bit longer than usual. That is normal for 3D models; it keeps checking by itself.";
  if (s.remote_state === "submission_uncertain") return "Checking whether Tripo received the job…";
  if (s.remote_state === "polling" || s.remote_state === "submitted") return "Tripo is working on it.";
  return s.message || "";
}

/** Why the automatic model was not used, without the check code: "CHK-M04: 6% of the surface is thinner than 0.05 stud" -> a sentence. @param {string} text */
function plainReason(text) {
  const t = String(text || "").replace(/^(CHK-[A-Z0-9]+|[A-Z]_[A-Z_]+):\s*/, "");
  if (/thinner than/.test(t)) return `The automatic model was too thin in places (${t}). Roblox needs at least 0.05 stud.`;
  if (/rotations|orientation|iou/i.test(t)) return "The model does not look like the approved pictures from any side, so it was not used. Check that it is the right file for this part, then drop it again.";
  if (/mirror/i.test(t)) return "The model looks mirrored. Check which picture you gave Tripo as Left and Right, or use Flip left/right once you have looked at it.";
  return /^[A-Z]/.test(t) ? t : t.charAt(0).toUpperCase() + t.slice(1);
}

/** @param {import("../router.js").PageContext} ctx */
export async function render(ctx) {
  const id = ctx.params.id;
  const body = h("div", { class: "page-body" });
  setChildren(ctx.root, pageHeader({ title: "Build", lead: "The approved parts are being made into real clothes and 3D pieces. You can leave this page; it keeps going.", back: { href: `#/p/${id}`, label: "Your duo" } }), body);
  /** @type {Set<string>} */
  const keepOpen = new Set();

  const draw = async () => {
    /** @type {any} */ let bundle; /** @type {any[]} */ let jobs = []; let gates = { data: /** @type {any[]} */ ([]), unavailable: false }; let inbox = { data: /** @type {any} */ (null), unavailable: false };
    try {
      [bundle, jobs] = await Promise.all([get(`/api/projects/${encodeURIComponent(id)}`, { signal: ctx.signal }), get("/api/jobs", { query: { project_id: id, steps: "true" }, signal: ctx.signal })]);
      gates = await tryGet("/api/gates", { query: { project_id: id, state: "open" }, signal: ctx.signal });
      inbox = await tryGet("/api/inbox", { signal: ctx.signal });
    } catch (err) { if (ctx.active()) setChildren(body, errorNote(friendly(err))); return; }
    if (!ctx.active()) return;
    const parts = /** @type {any[]} */ (bundle.parts || []);
    const project = bundle.project;
    const buildJobs = jobs.filter((j) => ["build", "manual_mesh", "duo", "parts"].includes(j.job.kind));
    const steps = /** @type {any[]} */ (buildJobs.flatMap((j) => j.steps || []));
    const manualGates = gates.data.filter((g) => g.kind === "manual_import");
    const panels = openGatePanels(id, gates.data.filter((g) => !["manual_import", "concept", "part_board", "final_pick"].includes(g.kind)), { refresh: draw, navigate: ctx.navigate });
    const done = steps.filter((s) => ["succeeded", "superseded"].includes(s.state)).length;
    const total = steps.filter((s) => s.state !== "superseded").length;

    const byPart = new Map();
    for (const s of steps) { const k = s.part_id || "_shared"; if (!byPart.has(k)) byPart.set(k, []); byPart.get(k).push(s); }
    /** One line of a part's timeline. @param {any} s */
    const stepRow = (s) => h("li", { class: `step ${s.state}` },
      h("span", { class: "step-main" }, h("strong", {}, stepLabel(s.kind))),
      badge(stepStateLabel(s.state), s.state === "succeeded" ? "ok" : s.state === "failed" ? "bad" : "busy"),
      ["running", "waiting_remote"].includes(s.state) ? progress(s.progress || 0, stepLabel(s.kind)) : null,
      stepNote(s) ? h("span", { class: "step-msg muted" }, stepNote(s)) : null,
      s.state === "failed" ? h("span", { class: "step-error" }, s.error?.user_hint || "This step did not work.", " ", h("button", { type: "button", class: "btn small", onclick: async () => { try { await post(`/api/steps/${s.id}/retry`); await draw(); } catch (e) { toast(friendly(e), { kind: "bad" }); } } }, "Try again")) : null);
    /** A part that has been drawn, checked and redrawn several times has dozens of finished steps: the earlier ones fold away, what is happening now stays in view. @param {string} pid @param {any[]} active */
    const timelineRows = (pid, active) => {
      const finishedSteps = active.filter((/** @type {any} */ s) => s.state === "succeeded");
      const folded = new Set(finishedSteps.length > FOLD_AFTER ? finishedSteps.slice(0, -KEEP_VISIBLE) : []);
      /** @type {any[]} */ const rows = [];
      /** @type {HTMLElement | null} */ let list = null;
      for (const s of active) {
        if (!folded.has(s)) { rows.push(stepRow(s)); continue; }
        if (!list) {
          list = h("ol", { class: "timeline" });
          const details = h("details", { class: "step-fold", open: keepOpen.has(`${pid}#fold`) }, h("summary", {}, `${folded.size} finished steps`), list);
          details.addEventListener("toggle", () => { if (details.open) keepOpen.add(`${pid}#fold`); else keepOpen.delete(`${pid}#fold`); });
          rows.push(h("li", { class: "step-fold-row" }, details));
        }
        list.append(stepRow(s));
      }
      return rows;
    };
    const partCards = [...byPart.entries()].map(([pid, list]) => {
      const part = parts.find((p) => p.id === pid);
      const active = list.filter((/** @type {any} */ s) => !["superseded"].includes(s.state));
      const failed = active.some((/** @type {any} */ s) => s.state === "failed");
      const finished = active.length && active.every((/** @type {any} */ s) => s.state === "succeeded");
      const d = h("details", { class: "build-part", open: keepOpen.has(pid) || !finished, dataset: { part: pid } },
        h("summary", {}, h("strong", {}, part?.label || (pid === "_shared" ? "Shared steps" : humanize(pid))), " ",
          badge(failed ? "Needs a retry" : finished ? "Done" : part ? stateLabel(part.state) : "Working", failed ? "bad" : finished ? "ok" : "busy")),
        h("ol", { class: "timeline" }, timelineRows(pid, active)),
        part && ["hair", "accessory"].includes(part.kind) ? validationBlock(id, part) : null);
      d.addEventListener("toggle", () => { if (d.open) keepOpen.add(pid); else keepOpen.delete(pid); });
      return d;
    });

    const unassigned = /** @type {any[]} */ (Array.isArray(inbox.data) ? inbox.data : inbox.data?.unassigned ?? inbox.data?.entries ?? inbox.data?.items ?? [])
      .filter((/** @type {any} */ e) => !e.assigned_part && !e.assigned && !e.part_id);
    setChildren(body, ...panels,
      note({ api: store.health?.demo ? "3D parts: a practice stand-in for Tripo makes them for you. Nothing is charged." : "3D parts: Tripo makes them for you through its service (uses your Tripo credits).", manual: "3D parts: you make them on Tripo's website. A pack is prepared for each one and you bring the file back.", ask: "3D parts: you will be asked how to make them when that step starts." }[/** @type {"api"|"manual"|"ask"} */ (project.settings?.mesh_mode ?? "ask")] ?? "", "info"),
      total ? panel({ title: "Progress" }, h("p", {}, `${done} of ${total} steps done`), progress(total ? done / total : 0, "Build progress")) : null,
      ...manualGates.map((g) => manualImportPanel(id, g, draw)),
      partCards.length ? h("div", { class: "build-parts" }, partCards)
        : emptyState("Nothing is being built yet", ["parts", "gate2", "brief", "planning", "gate1"].includes(project.stage) ? "The build starts when every part is approved." : "The steps appear here as they start.",
          h("a", { class: "btn", href: `#/p/${id}/board` }, "See the parts")),
      inbox.unavailable ? null : unassignedPanel(id, unassigned, parts, draw),
      project.stage === "gate3" ? panel({ class: "next-card" }, h("h2", {}, "The duo is ready"), h("a", { class: "btn primary big", href: `#/p/${id}/gate3` }, "Pick the final duo")) : null);
  };
  await draw();
  ctx.live(["step.state", "step.progress", "job.state", "gate.opened", "gate.updated", "part.state", "inbox.file"], () => { void draw(); }, 500);
}

/** Mesh validation results (triangles, box margins, orientation, mirrored flag), loaded when the user opens it. @param {string} projectId @param {any} part */
function validationBlock(projectId, part) {
  const host = h("div", { class: "validation" });
  const d = h("details", { class: "validation-details" }, h("summary", {}, "3D checks"), host);
  let loaded = false;
  d.addEventListener("toggle", async () => {
    if (!d.open || loaded) return;
    loaded = true;
    setChildren(host, h("p", { class: "muted" }, "Loading…"));
    try {
      const res = await tryGet(`/api/projects/${encodeURIComponent(projectId)}/parts/${encodeURIComponent(part.id)}`);
      if (res.unavailable) { setChildren(host, notAvailable("The 3D check list")); return; }
      const checks = /** @type {any[]} */ ((res.data?.checks ?? []).filter((/** @type {any} */ c) => /^(chk-)?m\d|mesh|tris|orient|mirror|box/i.test(String(c.id || c.check_id))));
      if (!checks.length) { setChildren(host, h("p", { class: "muted" }, "No 3D checks have run for this part yet.")); return; }
      setChildren(host, h("ul", { class: "checks" }, checks.map((c) => h("li", {}, badge(c.status === "passed" ? "Passed" : c.status === "not_applicable" ? "Does not apply" : "Needs a look", c.status === "passed" ? "ok" : c.status === "not_applicable" ? "muted" : "bad"), " ", h("strong", {}, checkLabel(String(c.check_id || c.id)))))));
    } catch (e) { setChildren(host, h("p", { class: "note warn" }, friendly(e))); }
  });
  return d;
}

/** @param {string} projectId @param {any} gate @param {() => Promise<void>} refresh */
function manualImportPanel(projectId, gate, refresh) {
  const tile = gate.tiles[0];
  const f = tile.facts || {};
  const packId = String(f.pack_id || f.pack || "");
  const folder = String(f.folder || f.pack_dir || "");
  const partId = tile.part_id || f.part_id || "";
  const slot = h("div", { class: "import-slot" });
  const status = h("p", { class: "muted", role: "status" }, f.inbox_status ? String(f.inbox_status) : f.reason && f.reason !== "made by hand" ? plainReason(f.reason) : "Waiting for your file. Drop it here, or save it in the pack's return folder.");
  const openFolder = h("button", { type: "button", class: "btn", onclick: async () => {
    try { await post("/api/os/open-folder", { kind: f.kind === "polish" ? "polish_pack" : "tripo_pack", id: packId }); toast("Opened the pack folder in Explorer.", { kind: "ok" }); } catch (e) {
      toast(e instanceof ApiError && e.unavailable ? "Opening folders is not available yet in this build. The folder path is shown above." : friendly(e), { kind: "warn" });
    }
  } }, "Open the pack folder");
  const dz = dropzone({ accept: ACCEPT, maxBytes: 50 * 1024 * 1024, title: "Drop your 3D file here, or press to choose it", hint: "GLB is best. FBX, zipped OBJ and Blender files work too if Blender is installed. Up to 50 MB.",
    onFiles: async ([file]) => { setChildren(slot, importWizard(projectId, partId, file, () => { setChildren(slot); void refresh(); }, dz)); } });
  const cancel = h("button", { type: "button", class: "btn quiet", onclick: async () => {
    if (!(await confirmDialog({ title: "Stop waiting for this file?", message: "You go back to the earlier choice. You can make a pack again later.", confirmLabel: "Cancel and go back" }))) return;
    const r = await decide(gate, tile, "cancel");
    if (r.status === "done") toast("Cancelled.", { kind: "info" });
    await refresh();
  } }, "Cancel and go back");
  return panel({ class: "gate-panel manual", title: `Make ${tile.label || "this part"} on Tripo's website` },
    note("On Tripo's FREE plan your model becomes public (CC BY 4.0) and has no commercial-use rights. Do not upload unreleased designs on the free plan.", "warn"),
    h("ol", { class: "steps-list" },
      h("li", {}, "Open the pack folder. It has the approved pictures and a SETTINGS.txt that says exactly what to choose."),
      h("li", {}, "Make the model on Tripo's website, download the GLB, and rename it to ", h("code", {}, packId ? `${packId}.glb` : "the pack name .glb"), "."),
      h("li", {}, "Drop it below, or save it in the pack's “return” folder. DuoSkin picks it up by itself.")),
    folder ? h("p", { class: "muted" }, "Folder: ", h("code", {}, folder)) : null,
    h("div", { class: "row" }, openFolder, cancel),
    f.settings_text ? h("details", {}, h("summary", {}, "What to choose on Tripo's website (these menu names may be out of date)"), h("pre", { class: "settings-text" }, String(f.settings_text))) : null,
    dz, status, slot);
}

/**
 * The import wizard for a file: preview it, ask which Tripo plan made it, then upload and assign.
 * @param {string} projectId @param {string} partId @param {File} file @param {() => void} done @param {any} dz
 */
function importWizard(projectId, partId, file, done, dz) {
  const name = `${uid("plan")}`;
  const plans = [["paid", "Tripo, a paid plan", "Private. Commercial use is allowed."], ["free", "Tripo, the free plan", "Public, labelled CC BY 4.0, and not for commercial use."], ["not_tripo", "Not from Tripo (I made it in Blender or elsewhere)", "Counted as made by you."]];
  const banner = h("p", { class: "note warn", role: "status", hidden: true }, FREE_BANNER);
  const group = h("fieldset", { class: "choice-group" }, h("legend", {}, "Which Tripo plan made this file?"),
    plans.map(([v, t, sub], i) => h("label", { class: "choice" }, h("input", { type: "radio", name: name, value: v, checked: i === 0, onchange: () => { banner.hidden = v !== "free"; } }), h("span", {}, h("strong", {}, t), h("br"), h("small", { class: "muted" }, sub)))));
  group.addEventListener("change", () => { const c = /** @type {HTMLInputElement | null} */ (group.querySelector("input:checked")); banner.hidden = c?.value !== "free"; });
  const link = h("input", { type: "url", placeholder: "Optional: the Tripo task link", "aria-label": "Tripo task link (optional)" });
  const err = h("div", { class: "msg" });
  const lower = file.name.toLowerCase();
  const previewable = lower.endsWith(".glb") || lower.endsWith(".fbx");
  const preview = previewable ? viewerPanel({ models: [{ file }], height: 280, caption: "Preview of your file (nothing is uploaded yet)" }) : null;
  const go = h("button", { type: "button", class: "btn primary" }, "Import this file");
  const cancel = h("button", { type: "button", class: "btn", onclick: () => { preview?.viewer.dispose(); done(); } }, "Choose a different file");
  go.addEventListener("click", async () => {
    go.disabled = true;
    setChildren(err);
    try {
      const fd = new FormData();
      fd.append("file", file, file.name);
      fd.append("project_id", projectId);
      if (partId) fd.append("part_id", partId);
      const entry = await post("/api/imports", fd);
      const inboxId = entry?.id ?? entry?.inbox_id;
      const plan = /** @type {HTMLInputElement} */ (group.querySelector("input:checked")).value;
      await post(`/api/imports/${encodeURIComponent(inboxId)}/assign`, { project_id: projectId, part_id: partId, tripo_plan: plan, task_link: link.value.trim() || undefined });
      toast("Imported. It is being checked and tidied now.", { kind: "ok" });
      preview?.viewer.dispose();
      dz.setStatus(`Imported ${file.name} (${fmtBytes(file.size)})`, "ok");
      done();
    } catch (e) {
      setChildren(err, e instanceof ApiError && e.unavailable ? note("Importing files is not available yet in this build.", "warn") : note(friendly(e), "bad"));
      go.disabled = false;
    }
  });
  return h("div", { class: "import-wizard panel" }, h("h3", {}, `Import ${file.name}`), h("p", { class: "muted" }, fmtBytes(file.size)), preview?.el, group, banner,
    h("div", { class: "field" }, h("label", {}, "Tripo task link (optional)"), link), err, h("div", { class: "row" }, cancel, go),
    h("p", { class: "muted small" }, "Until you answer the question above, the licence is recorded as unknown and the part cannot be marked ready to sell."));
}

/** @param {string} projectId @param {any[]} items @param {any[]} parts @param {() => Promise<void>} refresh */
function unassignedPanel(projectId, items, parts, refresh) {
  if (!items.length) return null;
  const candidates = parts.filter((p) => ["hair", "accessory"].includes(p.kind));
  return panel({ title: "Files that arrived but are not matched yet", lead: "Pick which part each file belongs to." },
    h("ul", { class: "plain-list" }, items.map((it) => {
      const sel = h("select", { "aria-label": `Part for ${it.name || it.filename}` }, h("option", { value: "" }, "Choose a part…"), candidates.map((p) => h("option", { value: p.id }, `${p.label || partKindLabel(p.kind)}`)));
      const btn = h("button", { type: "button", class: "btn small", onclick: async () => {
        if (!sel.value) { toast("Choose which part the file is for.", { kind: "warn" }); return; }
        try { await post(`/api/imports/${encodeURIComponent(it.id ?? it.inbox_id)}/assign`, { project_id: projectId, part_id: sel.value, tripo_plan: "not_tripo" }); toast("Matched. It is being checked now.", { kind: "ok" }); await refresh(); } catch (e) { toast(friendly(e), { kind: "bad" }); }
      } }, "Match it");
      return h("li", { class: "row" }, h("strong", {}, it.name || it.filename || "A file"), it.size ? h("span", { class: "muted" }, fmtBytes(it.size)) : null, sel, btn);
    })));
}
