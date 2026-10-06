// @ts-check
// Settings (#/settings/<tab>; APP_SPEC 12, 14): keys with "Test key" (masked status only), budgets, models, providers
// (real / practice / switched off, demo mode), 3D, checks, folders, storage, diagnostics and optional components.
import { get, put, post, friendly, ApiError } from "../api.js";
import { h, humanize, setChildren } from "../dom.js";
import { pageHeader, panel, field, switchField, tabList, note, notAvailable, badge, errorNote } from "../components/ui.js";
import { keysTable } from "../components/keys.js";
import { toast } from "../components/toast.js";
import { refreshHealth } from "../store.js";
import { providerLabel } from "../text.js";

const TABS = [
  { id: "keys", label: "Keys" }, { id: "budgets", label: "Budgets" }, { id: "models", label: "Models and versions" }, { id: "providers", label: "Providers" },
  { id: "3d", label: "3D" }, { id: "checks", label: "Checks" }, { id: "folders", label: "Folders" }, { id: "storage", label: "Storage" },
  { id: "diagnostics", label: "Diagnostics" }, { id: "optional", label: "Optional components" },
];
const MODE_WORDS = /** @type {Record<string, string>} */ ({ real: "Real (uses your key, costs money)", mock: "Practice (no key, no cost)", disabled: "Switched off" });

/** @param {import("../router.js").PageContext} ctx */
export async function render(ctx) {
  let tab = TABS.some((t) => t.id === ctx.params.tab) ? ctx.params.tab : "keys";
  /** @type {any} */ let settings;
  try { settings = await get("/api/settings", { signal: ctx.signal }); } catch (err) { setChildren(ctx.root, pageHeader({ title: "Settings" }), errorNote(friendly(err))); return; }
  if (!ctx.active()) return;
  const content = h("div", { class: "settings-body", role: "tabpanel", "aria-labelledby": `tab-${tab}` });
  const tabs = tabList(TABS, tab, (id) => {
    if (id === tab) return;
    tab = id;
    history.replaceState(null, "", `#/settings/${id}`);   // no full page change: keyboard focus stays on the tab
    content.setAttribute("aria-labelledby", `tab-${id}`);
    void draw();
  }, "Settings sections");
  setChildren(ctx.root, pageHeader({ title: "Settings", lead: "Changes save as soon as you make them." }), tabs, content);

  /** @param {Record<string, any>} patch @param {string} [msg] */
  const save = async (patch, msg = "Saved.") => {
    try { settings = await put("/api/settings", patch); toast(msg, { kind: "ok" }); void refreshHealth(); return true; } catch (e) { toast(friendly(e), { kind: "bad" }); return false; }
  };
  /** @param {string} label @param {number | null | undefined} value @param {(v: number | null) => Record<string, any>} patch @param {{hint?: string, min?: number, step?: number, nullable?: boolean}} [o] */
  const num = (label, value, patch, o = {}) => {
    const input = h("input", { type: "number", min: String(o.min ?? 0), step: String(o.step ?? 1), value: value == null ? "" : String(value), class: "narrow" });
    input.addEventListener("change", () => { const v = input.value === "" ? null : Number(input.value); if (v === null && !o.nullable) return; void save(patch(v)); });
    return field(label, input, o.hint);
  };
  /** @param {string} label @param {string} value @param {(v: string) => Record<string, any>} patch @param {string} [hint] */
  const text = (label, value, patch, hint) => {
    const input = h("input", { type: "text", value, class: "wide", spellcheck: "false" });
    input.addEventListener("change", () => { void save(patch(input.value.trim())); });
    return field(label, input, hint);
  };
  /** @param {string} label @param {boolean} value @param {(v: boolean) => Record<string, any>} patch @param {string} [hint] */
  const sw = (label, value, patch, hint) => switchField({ label, hint, checked: value, onChange: (v) => { void save(patch(v)); } });
  /** @param {string} label @param {string} value @param {[string, string][]} options @param {(v: string) => Record<string, any>} patch @param {string} [hint] */
  const sel = (label, value, options, patch, hint) => {
    const s = h("select", {}, options.map(([v, t]) => h("option", { value: v, selected: v === value }, t)));
    s.addEventListener("change", () => { void save(patch(s.value)); });
    return field(label, s, hint);
  };

  const views = /** @type {Record<string, () => Promise<HTMLElement | HTMLElement[]> | HTMLElement | HTMLElement[]>} */ ({
    keys: async () => [
      panel({}, sw("Demo mode (no keys needed)", settings.demo_mode, (v) => ({ demo_mode: v }), "Every service is a practice stand-in, so nothing is charged and nothing can be exported. A banner reminds you at the top.")),
      panel({ title: "Your keys" }, await keysTable({ onChange: () => { void refreshHealth(); } })),
    ],
    budgets: () => panel({ title: "Budgets", lead: "These are the starting values for each new duo. Each duo keeps its own copy, which you can change on its brief." }, h("div", { class: "grid-2" },
      num("Budget cap per duo ($)", settings.budgets.per_duo_usd, (v) => ({ budgets: { per_duo_usd: v } }), { min: 1, step: 0.5, hint: "DuoSkin stops and asks before going past this." }),
      num("Ask me before a single step over ($)", settings.budgets.ask_above_usd, (v) => ({ budgets: { ask_above_usd: v } }), { step: 0.5, hint: "Bigger steps always wait for your yes." }),
      num("Daily limit across all duos ($)", settings.budgets.daily_cap_usd, (v) => ({ budgets: { daily_cap_usd: v } }), { min: 1, step: 1, nullable: true, hint: "Leave empty for no daily limit." }),
      num("Warn me when Tripo credits fall below", settings.budgets.tripo_credit_floor, (v) => ({ budgets: { tripo_credit_floor: v } }), { hint: "Credits left after what is already in use." }),
      num("Ask before a regression test costing more than ($)", settings.budgets.regression_ask_usd, (v) => ({ budgets: { regression_ask_usd: v } }), { min: 1, step: 1, hint: "These tests never run without your confirmation." }))),
    models: () => {
      const m = settings.models;
      const roles = Object.keys(m).filter((k) => k !== "candidates");
      const cand = m.candidates || {};
      return panel({ title: "Models and versions", lead: "These are the exact versions new duos use. A candidate becomes the default only after the regression test passes (see Learning)." },
        h("div", { class: "table-wrap" }, h("table", { class: "table" }, h("thead", {}, h("tr", {}, ["What it is used for", "Version in use", "Candidate waiting for the test"].map((c) => h("th", {}, c)))),
          h("tbody", {}, roles.map((r) => {
            const input = h("input", { type: "text", value: cand[r] ?? "", placeholder: "none", "aria-label": `Candidate for ${humanize(r)}`, spellcheck: "false", class: "wide" });
            input.addEventListener("change", async () => {
              const v = input.value.trim();
              if (/latest/i.test(v)) { toast("Use the dated version name, not “latest”, so a result can always be repeated.", { kind: "warn" }); input.value = cand[r] ?? ""; return; }
              await save({ models: { candidates: { [r]: v || null } } }, v ? "Candidate saved. It is not used until the regression passes." : "Candidate removed.");
            });
            return h("tr", {}, h("td", {}, humanize(r)), h("td", {}, h("code", {}, m[r])), h("td", {}, input));
          })))),
        note("Aliases such as “latest” are refused: every version must be a dated snapshot, so a result can always be repeated.", "info"));
    },
    providers: () => [
      panel({}, sw("Demo mode (no keys needed)", settings.demo_mode, (v) => ({ demo_mode: v }), "All services become practice stand-ins and a banner stays at the top.")),
      panel({ title: "Each service", lead: "Real uses your key and costs money. Practice makes stand-in pictures for free. Switched off means that service is never called." },
        h("div", { class: "table-wrap" }, h("table", { class: "table" }, h("thead", {}, h("tr", {}, ["Service", "How it runs"].map((c) => h("th", {}, c)))),
          h("tbody", {}, Object.entries(settings.providers.modes).map(([p, mode]) => {
            const s = h("select", { "aria-label": `${providerLabel(p)} mode` }, ["real", "mock", "disabled"].map((v) => h("option", { value: v, selected: v === mode }, MODE_WORDS[v])));
            s.addEventListener("change", () => { void save({ providers: { modes: { [p]: s.value } } }); });
            return h("tr", {}, h("td", {}, providerLabel(p)), h("td", {}, s));
          }))))),
      panel({ title: "Speed limits", lead: "Only change these if a service tells you it is being asked too fast." }, h("div", { class: "grid-2" },
        num("Pictures per minute (OpenAI)", settings.providers.openai_ipm, (v) => ({ providers: { openai_ipm: v } }), { min: 1 }),
        num("Claude calls at once", settings.providers.anthropic_concurrency, (v) => ({ providers: { anthropic_concurrency: v } }), { min: 1 }),
        num("OpenAI calls at once", settings.providers.openai_concurrency, (v) => ({ providers: { openai_concurrency: v } }), { min: 1 }),
        num("Recraft calls at once", settings.providers.recraft_concurrency, (v) => ({ providers: { recraft_concurrency: v } }), { min: 1 }),
        num("Tripo models at once", settings.providers.tripo_slots, (v) => ({ providers: { tripo_slots: v } }), { min: 1 })),
        sw("My Gemini key is a paid one", settings.providers.gemini_key_billed, (v) => ({ providers: { gemini_key_billed: v } }), "Private pictures are only sent to Gemini when this is on.")),
      Object.keys(settings.capabilities || {}).length ? panel({ title: "What the services told us they can do" }, h("ul", { class: "plain-list" }, Object.entries(settings.capabilities).map(([k, v]) => h("li", {}, humanize(k.replace(/\./g, " ")), ": ", badge(v === true ? "Yes" : v === false ? "No" : String(v), v === true ? "ok" : "muted"))))) : null,
    ],
    "3d": () => panel({ title: "3D parts" },
      sel("When a 3D part is needed", settings.three_d.default_mesh_mode, [["ask", "Ask me each time (recommended)"], ["api", "Let Tripo make it automatically"], ["manual", "I make it on Tripo's website"]], (v) => ({ three_d: { default_mesh_mode: v } }), "You can still choose per duo."),
      sel("Hair route", settings.three_d.hair_default_route, [["auto", "Automatic"], ["kit", "Use the hair kit"], ["tripo_api", "Tripo makes it"], ["manual", "I make it myself"]], (v) => ({ three_d: { hair_default_route: v } })),
      text("Blender program (optional)", settings.three_d.blender_path ?? "", (v) => ({ three_d: { blender_path: v || null } }), "Leave empty if you do not have Blender. Without it, FBX and Blender files cannot be opened, so export them as GLB. Blender 4.2 or newer is recommended."),
      sel("Which way Roblox Studio faces", settings.three_d.studio_forward_axis, [["unknown", "Not tested yet"], ["+Z", "+Z"], ["-Z", "-Z"]], (v) => ({ three_d: { studio_forward_axis: v } }), "Set by the calibration test; leave as it is unless you ran it."),
      sw("Start building each part as soon as I approve it", settings.three_d.build_start_per_tile, (v) => ({ three_d: { build_start_per_tile: v } }), "Off means the build starts once every part is approved.")),
    checks: () => panel({ title: "Checks" },
      sw("Check similarity to my reference pictures", settings.checks.reference_similarity_default, (v) => ({ checks: { reference_similarity_default: v } }), "Off by default. This is only the starting value for new duos; you can switch it per duo."),
      sw("Allow the sparkle-star highlight in eyes", settings.checks.sparkle_star_allowed, (v) => ({ checks: { sparkle_star_allowed: v } })),
      sw("Compare declared and built proportions", settings.checks.ratio_check, (v) => ({ checks: { ratio_check: v } })),
      sw("Cheap judging (fewer plans are judged)", settings.checks.cheap_critic_mode, (v) => ({ checks: { cheap_critic_mode: v } }), "Saves money; the judge looks at the best two plans only."),
      sw("Ask Gemini for a second opinion", settings.checks.gemini_second_opinion, (v) => ({ checks: { gemini_second_opinion: v } }), "Needs a paid Gemini key."),
      sel("Face drawing experiment", settings.checks.face_route_ab, [["off", "Off"], ["r1_vs_i3", "Compare two ways of drawing faces"]], (v) => ({ checks: { face_route_ab: v } })),
      Object.keys(settings.checks.check_overrides || {}).length ? h("div", {}, h("h3", {}, "Checks turned into suggestions"), h("ul", { class: "plain-list" }, Object.keys(settings.checks.check_overrides).map((k) => h("li", {}, humanize(k), " ", h("button", { type: "button", class: "btn small", onclick: async () => { await save({ checks: { check_overrides: { [k]: null } } }); void draw(); } }, "Make it a required check again"))))) : null),
    folders: () => panel({ title: "Folders", lead: "Where DuoSkin puts what it makes. Windows variables such as %USERPROFILE% are fine." },
      text("Exports", settings.paths.exports_root, (v) => ({ paths: { exports_root: v } })),
      text("Tripo inbox (where your own 3D files can be dropped)", settings.paths.tripo_inbox, (v) => ({ paths: { tripo_inbox: v } })),
      h("div", { class: "row" }, ["export", "inbox", "logs"].map((k) => h("button", { type: "button", class: "btn", onclick: async () => {
        try { await post("/api/os/open-folder", { kind: k, id: "" }); } catch (e) { toast(e instanceof ApiError && e.unavailable ? "Opening folders is not available yet in this build." : friendly(e), { kind: "warn" }); }
      } }, `Open the ${k === "export" ? "exports" : k === "inbox" ? "inbox" : "log"} folder`)))),
    storage: () => panel({ title: "Storage" }, notAvailable("The disk-use report and the tidy-up preview"), h("p", { class: "muted" }, "Pictures and 3D files are kept by content, so the same picture is never stored twice.")),
    diagnostics: async () => {
      const result = h("div", { role: "status" });
      const run = h("button", { type: "button", class: "btn primary", onclick: async () => {
        run.disabled = true;
        try { const r = await post("/api/diagnostics"); setChildren(result, note("Done. The file has no keys in it: send it when you ask for help.", "ok"), h("p", {}, "Saved as: ", h("code", {}, String(r.path)))); } catch (e) { setChildren(result, note(friendly(e), "bad")); }
        run.disabled = false;
      } }, "Make a diagnostics file");
      const health = await get("/api/health").catch(() => null);
      return [panel({ title: "Diagnostics", lead: "A zip with the logs and the doctor report, with every key removed, for when something needs fixing." }, run, result),
        panel({ title: "About" }, h("p", {}, `DuoSkin Studio ${health?.version ?? ""}`), h("p", { class: "muted" }, `Listening on this computer only (port ${settings.port}). Nothing leaves your PC except calls to the services you switched on.`),
          h("p", {}, h("a", { class: "btn", href: "#/setup" }, "Run the setup check again")))];
    },
    optional: async () => {
      const doctor = await get("/api/doctor").catch(() => null);
      const comps = [["DreamSim", "dreamsim", "Compares how alike two designs look. Without it a simpler check is used and labelled “clone check degraded”."], ["Background matting", "matting", "Cleans the edges of pictures. Without it, simpler edge cleaning is used."], ["Extra text-reading models", "ocr", "Finds stray text in pictures. The built-in reader is always on."]];
      return panel({ title: "Optional components", lead: "Nothing here is required. Each one only makes a check better." },
        h("ul", { class: "plain-list" }, comps.map(([name, key, what]) => {
          const hit = (doctor?.checks ?? []).find((/** @type {any} */ c) => String(c.id).toLowerCase().includes(key));
          return h("li", {}, h("strong", {}, name), " ", hit ? badge(hit.status === "pass" ? "Installed" : "Not installed", hit.status === "pass" ? "ok" : "muted") : badge("Not checked", "muted"), h("br"), h("span", { class: "muted" }, what));
        })), notAvailable("One-click download of components", "For now, run the setup check to see what is installed."));
    },
  });
  let drawn = 0;                                       // a slow tab must not paint over the tab chosen after it
  const draw = async () => {
    const mine = ++drawn;
    try {
      const out = await views[tab]();
      if (mine === drawn && ctx.active()) setChildren(content, ...(Array.isArray(out) ? out : [out]).filter(Boolean));
    } catch (e) { if (mine === drawn && ctx.active()) setChildren(content, errorNote(friendly(e))); }
  };
  await draw();
}
