// @ts-check
// Gate 2: the part board (#/p/<id>/board; APP_SPEC 9.3, 10). Two columns, A and B; every part is a tile shown alone with
// Approve / Reimagine / Change…; "Approve all remaining" and "Back to concept" at the top.
import { get, tryGet, post, friendly } from "../api.js";
import { h, setChildren } from "../dom.js";
import { pageHeader, panel, emptyState, errorNote, notAvailable, progress, badge } from "../components/ui.js";
import { tile, partKindOf, characterOf, hardFailuresOf } from "../components/tile.js";
import { openTileDrawer } from "../components/drawer.js";
import { runTileAction } from "../components/tile-actions.js";
import { decide } from "../components/decisions.js";
import { confirmDialog } from "../components/modal.js";
import { dnaCard } from "../components/dnacard.js";
import { openGatePanels } from "../components/gatepanels.js";
import { toast } from "../components/toast.js";
import { stateLabel, partKindLabel, nextAction } from "../text.js";

const ORDER = ["colours", "face", "hair", "accessory", "print", "shirt", "pants"];

/** @param {any} t */
function rank(t) {
  const k = ORDER.indexOf(partKindOf(t));
  return (k < 0 ? 99 : k) * 100 + (parseInt((t.part_id || "").split(".").pop() || "0", 10) || 0);
}

/** @param {import("../router.js").PageContext} ctx */
export async function render(ctx) {
  const id = ctx.params.id;
  const body = h("div", {});
  setChildren(ctx.root, pageHeader({ title: "The parts", lead: "Every part is shown alone. Approve the ones you like, or ask for a new try. Only approved parts get built.", back: { href: `#/p/${id}`, label: "Your duo" } }), body);

  const draw = async () => {
    /** @type {any} */ let bundle; let gates = { data: /** @type {any[]} */ ([]), unavailable: false };
    try {
      bundle = await get(`/api/projects/${encodeURIComponent(id)}`, { signal: ctx.signal });
      gates = await tryGet("/api/gates", { query: { project_id: id, state: "open" }, signal: ctx.signal });
    } catch (err) { if (ctx.active()) setChildren(body, errorNote(friendly(err))); return; }
    if (!ctx.active()) return;
    const project = bundle.project;
    if (gates.unavailable) { setChildren(body, notAvailable("The part board")); return; }
    const board = gates.data.find((g) => g.kind === "part_board");
    const panels = openGatePanels(id, gates.data.filter((g) => g.kind !== "part_board" && g.kind !== "concept"), { refresh: draw, navigate: ctx.navigate });
    const parts = /** @type {any[]} */ (bundle.parts || []);
    if (!board) {
      const na = nextAction(project, gates.data);
      const approved = parts.filter((p) => ["approved", "built"].includes(p.state)).length;
      const allDone = parts.length > 0 && parts.every((p) => ["approved", "building", "built"].includes(p.state));
      setChildren(body, ...panels, allDone ? panel({ class: "next-card" }, h("h2", {}, "Every part is approved"), h("p", {}, "The build is under way: the clothes, the 3D parts and the head."), h("a", { class: "btn primary big", href: `#/p/${id}/build` }, "Watch the build")) : null, parts.length
        ? panel({ title: project.stage === "parts" ? "The parts are being made" : "The parts" }, project.stage === "parts" ? h("p", { class: "muted" }, "Each part appears here as soon as it is ready for you to look at.") : null,
          progress(parts.length ? approved / parts.length : 0, "Parts approved"),
          h("ul", { class: "plain-list" }, parts.map((p) => h("li", {}, h("strong", {}, p.label || partKindLabel(p.kind)), " ", badge(stateLabel(p.state), p.state === "approved" || p.state === "built" ? "ok" : "muted")))),
          allDone ? null : h("a", { class: "btn primary", href: na.href }, na.label))
        : emptyState("No parts yet", "The parts appear after you approve a concept.", h("a", { class: "btn primary", href: na.href }, na.label)));
      return;
    }
    const firstChoice = Boolean(board.first_choice_at);
    const tiles = /** @type {any[]} */ ([...board.tiles].sort((a, b) => rank(a) - rank(b)));
    const approvedCount = tiles.filter((t) => t.state === "approved").length;
    const partLabels = Object.fromEntries(parts.map((p) => [p.id, p.label]));
    const readyCount = tiles.filter((t) => t.state === "ready" && !hardFailuresOf(t).length).length;
    const warningBudget = { left: 2 };
    /** @param {any} t */
    const makeTile = (t) => {
      const env = { projectId: id, gate: board, tile: t, refresh: draw, partLabels, navigate: ctx.navigate };
      const open = () => {
        if (t.part_id) void post("/api/focus", { project_id: id, part_id: t.part_id }).catch(() => {});
        openTileDrawer(env, firstChoice);
      };
      return tile({ tile: t, firstChoice, warningBudget, onOpen: open, onAction: async (action) => { await runTileAction(env, action); } });
    };
    const colTiles = (/** @type {"a"|"b"} */ c) => tiles.filter((t) => characterOf(t.part_id || "") === c);
    const rows = 1 + Math.max(colTiles("a").length, colTiles("b").length);
    const column = (/** @type {"a"|"b"} */ c) => {
      const el = h("section", { class: `board-col char-${c}`, "aria-label": `Character ${c.toUpperCase()}` },
        h("h2", {}, h("span", { class: `char-chip ${c}` }, c.toUpperCase()), `Character ${c.toUpperCase()}`), colTiles(c).map(makeTile));
      el.style.setProperty("--rows", String(rows));
      return el;
    };
    const shared = tiles.filter((t) => !characterOf(t.part_id || ""));
    const approveAll = h("button", { type: "button", class: "btn primary", disabled: !readyCount, title: "Approves every part that is ready and has no failed required check", onclick: async () => {
      const r = await decide(board, tiles[0], "approve_all", { what: "all remaining parts" });
      if (r.status === "done") toast("Approved everything that was ready.", { kind: "ok" });
      await draw();
    } }, `Approve all remaining (${readyCount})`);
    const back = h("button", { type: "button", class: "btn", onclick: async () => {
      if (!(await confirmDialog({ title: "Go back to the concept?", message: "Approved parts only need another look where the new concept changes them. Nothing is deleted.", confirmLabel: "Back to concept" }))) return;
      const r = await decide(board, tiles[0], "back_to_concept");
      if (r.status === "done") ctx.navigate(`/p/${id}/gate1`);
      else await draw();
    } }, "Back to concept");
    const dna = bundle.dna_card;
    setChildren(body, ...panels,
      h("section", { class: "board-top" },
        h("div", { class: "board-progress" }, h("p", {}, h("strong", {}, `${approvedCount} of ${tiles.length} parts approved`)), progress(tiles.length ? approvedCount / tiles.length : 0, "Parts approved")),
        h("div", { class: "row" }, approveAll, back)),
      dna ? h("details", { class: "dna-details" }, h("summary", {}, "Design card and colours"), dnaCard(dna, { compact: false, title: "Design card" })) : null,
      approvedCount === tiles.length && tiles.length ? panel({ class: "next-card" }, h("h2", {}, "Every part is approved"), h("p", {}, "The build starts now: the clothes, the 3D parts and the head."), h("a", { class: "btn primary big", href: `#/p/${id}/build` }, "Watch the build")) : null,
      h("div", { class: "board-cols aligned" }, column("a"), column("b")),
      shared.length ? h("section", { class: "board-shared" }, h("h2", {}, "Shared"), h("div", { class: "board-cols" }, shared.map(makeTile))) : null);
  };
  await draw();
  ctx.live(["gate.opened", "gate.updated", "tile.updated", "part.state", "warning.released"], () => { void draw(); }, 400);
}
