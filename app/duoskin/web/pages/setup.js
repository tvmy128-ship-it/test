// @ts-check
// Setup wizard (#/setup; APP_SPEC 12): 1 keys, 2 the doctor, 3 Blender, 4 kits, 5 taste, 6 house style, 7 test day.
// Nothing here ever blocks: every step can be skipped and says what happens without it.
import { get, put, post, tryGet, friendly } from "../api.js";
import { h, humanize, setChildren } from "../dom.js";
import { pageHeader, panel, badge, note, notAvailable, switchField, errorNote, emptyState } from "../components/ui.js";
import { keysTable } from "../components/keys.js";
import { toast } from "../components/toast.js";
import { openGatePanels } from "../components/gatepanels.js";
import { refreshHealth, state as store } from "../store.js";

const STEPS = [
  ["keys", "Keys"], ["doctor", "Check this computer"], ["blender", "Blender"], ["kits", "Kits"], ["taste", "Your taste"], ["house", "House style"], ["test", "Test day"],
];
const KIT_WITHOUT = /** @type {Record<string, [string, string]>} */ ({
  head_base_present: ["Head base", "Faces are shown as flat pictures on a plain head, labelled “2D preview, no head base”."],
  body_base_present: ["Body base", "The standard Block body is used."],
  hair_kit_empty: ["Hair kit", "Every hairstyle is made as a custom 3D part (Tripo or your own file)."],
  house_style_present: ["House style sheet", "Pictures are made without a style sheet; results can vary more."],
  fabrics: ["Fabric tiles", "A procedural fabric is used."],
  fold_sets: ["Fold sets", "Soft procedural folds are drawn by the program, labelled “procedural folds”."],
  dreamsim_present: ["Similarity model (DreamSim)", "A simpler check is used and labelled “clone check degraded”."],
  blender_present: ["Blender", "FBX files are not produced and only GLB files can be imported."],
});

/** @param {import("../router.js").PageContext} ctx */
export async function render(ctx) {
  let step = Math.max(0, Math.min(STEPS.length - 1, Number(ctx.query.get("step") || 0)));
  const body = h("div", { class: "setup-body" });
  const nav = h("nav", { class: "setup-steps", "aria-label": "Setup steps" });
  setChildren(ctx.root, pageHeader({ title: "Set up DuoSkin Studio", lead: "Seven short steps. None of them blocks you: you can skip any of them and come back later.", back: { href: "#/", label: "Your duos" } }), nav, body);

  const go = (/** @type {number} */ n) => { step = Math.max(0, Math.min(STEPS.length - 1, n)); history.replaceState(null, "", `#/setup?step=${step}`); void draw(); window.scrollTo({ top: 0 }); };
  const footer = () => h("div", { class: "row setup-foot" },
    step > 0 ? h("button", { type: "button", class: "btn", onclick: () => go(step - 1) }, "Back") : null,
    step < STEPS.length - 1 ? h("button", { type: "button", class: "btn primary", onclick: () => go(step + 1) }, "Next") : h("a", { class: "btn primary", href: "#/new" }, "Start my first duo"),
    step < STEPS.length - 1 ? h("button", { type: "button", class: "btn quiet", onclick: () => go(step + 1) }, "Skip this step") : null);

  /** @type {Record<string, () => Promise<HTMLElement[]>>} */
  const steps = {
    keys: async () => {
      const s = await get("/api/settings");
      const demo = switchField({ label: "Try it first in demo mode", hint: "Practice pictures, no keys, no cost. Nothing can be exported in demo mode. You can turn it off any time in Settings.", checked: s.demo_mode,
        onChange: async (v) => { try { await put("/api/settings", { demo_mode: v }); toast(v ? "Demo mode is on." : "Demo mode is off.", { kind: "ok" }); void refreshHealth(); } catch (e) { toast(friendly(e), { kind: "bad" }); } } });
      return [panel({ title: "1. Your keys", lead: "Keys let DuoSkin use the services that draw and judge. Paste each one and press Test key." }, demo, await keysTable({ onChange: () => { void refreshHealth(); } })), footer()];
    },
    doctor: async () => {
      /** @type {any} */ let d = await get("/api/doctor");
      const host = h("div", {});
      const paint = () => {
        const run = h("button", { type: "button", class: "btn primary", disabled: d.running, onclick: async () => { run.disabled = true; run.textContent = "Checking…"; try { await post("/api/doctor/run"); } catch (e) { toast(friendly(e), { kind: "bad" }); } } }, d.running ? "Checking…" : d.ran ? "Check again" : "Check this computer");
        const rows = d.ran ? /** @type {any[]} */ (d.checks || []) : [];
        const bad = rows.filter((c) => c.status === "fail");
        const warn = rows.filter((c) => c.status === "warn");
        setChildren(host, h("div", { class: "row" }, run, d.ran ? (d.blocks_paid_features ? badge("Paid features are blocked until the problems are fixed", "bad") : badge(`${d.summary?.passed ?? 0} passed, ${d.summary?.warnings ?? warn.length} to look at`, bad.length ? "bad" : warn.length ? "warn" : "ok")) : null),
          d.ran ? h("ul", { class: "doctor-list" }, [...bad, ...warn].map((c) => h("li", {}, badge(c.status === "fail" ? "Needs fixing" : "Take a look", c.status === "fail" ? "bad" : "warn"), " ", h("strong", {}, humanize(c.id)), h("br"), c.message, c.fix ? h("div", { class: "muted small" }, "What to do: " + c.fix) : null))) : h("p", { class: "muted" }, "This looks at your computer, folders and tools. Missing optional things are only warnings."),
          d.ran && !bad.length && !warn.length ? note("Everything checks out.", "ok") : null);
      };
      paint();
      ctx.on("doctor.result", async () => { d = await get("/api/doctor"); paint(); });
      const poll = setInterval(async () => { if (d.running) { d = await get("/api/doctor").catch(() => d); paint(); } }, 2000);
      ctx.onCleanup(() => clearInterval(poll));
      return [panel({ title: "2. Check this computer", lead: "Missing kits and optional programs are warnings, never a block." }, host), footer()];
    },
    blender: async () => {
      const s = await get("/api/settings");
      const input = h("input", { type: "text", class: "wide", value: s.three_d.blender_path ?? "", placeholder: "Found automatically if you used the default install folder", spellcheck: "false", "aria-label": "Blender program path" });
      const save = h("button", { type: "button", class: "btn", onclick: async () => { try { await put("/api/settings", { three_d: { blender_path: input.value.trim() || null } }); toast("Saved.", { kind: "ok" }); } catch (e) { toast(friendly(e), { kind: "bad" }); } } }, "Save");
      return [panel({ title: "3. Blender (optional)" },
        h("p", {}, "You do not need Blender to plan, build clothes, make 3D parts through Tripo, or export glTF."),
        h("p", {}, "Blender becomes needed only to: open FBX, zipped OBJ or .blend files you made yourself, produce the FBX backup in the export, or build a head base."),
        h("div", { class: "field" }, h("label", {}, "Blender program (optional)"), h("div", { class: "row" }, input, save)),
        h("details", {}, h("summary", {}, "How to install it"), h("ol", { class: "steps-list" }, h("li", {}, "Press the Windows key, type “cmd” and open Command Prompt."), h("li", {}, "Type ", h("code", {}, "winget install BlenderFoundation.Blender"), " and press Enter. (Or get the Windows installer from blender.org/download.)"), h("li", {}, "Keep the default install folder so DuoSkin finds it by itself."))),
        h("p", { class: "muted" }, "Recommended: Blender 5.2 LTS. The oldest that works is 4.2.")), footer()];
    },
    kits: async () => {
      const lib = await tryGet("/api/library");
      const flags = lib.data?.availability ?? lib.data?.flags ?? lib.data ?? {};
      const rows = Object.entries(KIT_WITHOUT).map(([k, [label, without]]) => {
        const v = k === "fabrics" || k === "fold_sets" ? (lib.data ?? {})[k] : flags[k];
        const present = k === "hair_kit_empty" ? v === false : k === "fabrics" || k === "fold_sets" ? Array.isArray(v) && v.length > 0 : v === true;
        const known = v !== undefined;
        return h("li", { class: "kit-row" }, h("strong", {}, label), " ", known ? badge(present ? "Ready" : "Not added", present ? "ok" : "muted") : badge("Unknown", "muted"), h("br"), present ? null : h("span", { class: "muted" }, "Without it: " + without));
      });
      return [panel({ title: "4. Kits", lead: "Kits are the building blocks: head, hair, body and fabrics. None are required; every missing one has a fallback." },
        lib.unavailable ? notAvailable("The kit list", "Each missing kit would use the fallback below.") : null, h("ul", { class: "plain-list kit-list" }, rows),
        h("p", {}, h("a", { class: "btn", href: "#/library" }, "Open the Library"))), footer()];
    },
    taste: async () => [panel({ title: "5. Your taste (optional)", lead: "Ten to twenty pictures you love, plus about fifty quick ratings, teach the planner what you like." },
      h("p", {}, "You can add favourites from the Library and rate designs in Calibration whenever you like. Skipping this only means the first plans are less personal."),
      h("div", { class: "row" }, h("a", { class: "btn", href: "#/library" }, "Add favourites in the Library"), h("a", { class: "btn", href: "#/calibration" }, "Open Calibration"))), footer()],
    house: async () => {
      const gates = await tryGet("/api/gates", { query: { state: "open" } });
      const setupGates = /** @type {any[]} */ ((gates.data ?? []).filter((/** @type {any} */ g) => g.kind === "setup_approval"));
      return [panel({ title: "6. House style (optional)", lead: "The house style is the look every picture follows. It can start without a style sheet." },
        setupGates.length ? h("div", {}, ...openGatePanels("", setupGates, { refresh: () => { void draw(); }, navigate: ctx.navigate })) : emptyState("Nothing to pick right now", "When examples are ready they appear here for you to approve. Starting a duo without a style sheet works fine."),
        store.health?.demo ? note("Demo mode uses built-in practice styles.", "info") : null), footer()];
    },
    test: async () => [panel({ title: "7. Test day (optional)", lead: "A short list of checks with your real keys that tell DuoSkin what each service can do. Each one says its cost and asks first." },
      note("Expect about $50 and about 450 Tripo credits for the full list, an estimate. You can skip it: DuoSkin learns the same things from its first real runs, one error at a time.", "info"),
      notAvailable("The test-day runner"),
      h("p", { class: "muted" }, "Until it is ready you can go straight to your first duo.")), footer()],
  };

  const draw = async () => {
    setChildren(nav, h("ol", { class: "setup-list" }, STEPS.map(([, label], i) => h("li", { class: i === step ? "current" : i < step ? "done" : "" },
      h("button", { type: "button", class: "setup-step", "aria-current": i === step ? "step" : null, onclick: () => go(i) }, h("span", { class: "stage-mark", "aria-hidden": "true" }, i < step ? "✓" : String(i + 1)), h("span", {}, label))))));
    ctx.setTitle(`Set up: ${STEPS[step][1]}`);
    try { setChildren(body, ...(await steps[STEPS[step][0]]())); } catch (e) { setChildren(body, errorNote(friendly(e))); }
  };
  await draw();
}
