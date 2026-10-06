// @ts-check
// New duo / Brief (#/new): the idea, the pair, optional pictures, the budget, then "Start the plan".
// The brief text is data: it is shown and stored as typed and never becomes an instruction.
import { get, post, friendly, ApiError } from "../api.js";
import { h, uid, setChildren } from "../dom.js";
import { pageHeader, panel, field, switchField, note } from "../components/ui.js";
import { dropzone } from "../components/dropzone.js";
import { PAIR_STRUCTURES } from "../text.js";
import { state } from "../store.js";

const COMBOS = [
  ["bg", "Boy + Girl", "A is the boy, B is the girl"],
  ["gb", "Girl + Boy", "A is the girl, B is the boy"],
  ["bb", "Boy + Boy", "Two boys"],
  ["gg", "Girl + Girl", "Two girls"],
];
const MAX_REFS = 4;

/** @param {import("../router.js").PageContext} ctx */
export async function render(ctx) {
  /** @type {any} */
  let defaults = null;
  try { defaults = await get("/api/settings", { signal: ctx.signal }); } catch { defaults = null; }
  if (!ctx.active()) return;
  const demo = Boolean(state.health?.demo ?? state.snapshot?.demo ?? defaults?.demo_mode);

  const name = h("input", { type: "text", maxlength: "60", autocomplete: "off", placeholder: "For example: Plush Koi", required: true });
  const brief = h("textarea", { rows: "5", maxlength: "2000", placeholder: "Describe the idea in your own words. For example: a calm koi-pond duo, one in a soft hoodie with a plush koi bag, the other in a lighter jacket with a lantern charm." });
  const briefCount = h("span", { class: "counter" }, "0 / 2000");
  brief.addEventListener("input", () => { briefCount.textContent = `${brief.value.length} / 2000`; });
  const combos = h("fieldset", { class: "combo-picker" }, h("legend", {}, "Who are the two characters?"),
    h("div", { class: "combo-grid" }, COMBOS.map(([v, label, sub], i) => {
      const id = uid("combo");
      return h("label", { class: "combo-card", for: id }, h("input", { type: "radio", name: "combo", id, value: v, checked: i === 0 }), h("span", { class: "combo-label" }, label), h("span", { class: "combo-sub" }, sub));
    })),
    h("p", { class: "hint" }, "The first one is character A, the second is character B."));
  const structure = h("select", {}, PAIR_STRUCTURES.map(([v, t]) => h("option", { value: v }, t)));

  // must-include lines (up to 5 lines of at most 12 words)
  const mustList = h("ul", { class: "must-list" });
  /** @param {string} [text] */
  const addMust = (text = "") => {
    if (mustList.children.length >= 5) return;
    const input = h("input", { type: "text", value: text, placeholder: "For example: a koi on the back of the jacket", "aria-label": `Must-include line ${mustList.children.length + 1}`, maxlength: "120" });
    const words = h("span", { class: "counter" }, "0 / 12 words");
    const upd = () => { const n = input.value.trim().split(/\s+/).filter(Boolean).length; words.textContent = `${n} / 12 words`; words.classList.toggle("bad", n > 12); };
    input.addEventListener("input", upd);
    const li = h("li", {}, input, words, h("button", { type: "button", class: "btn quiet small", onclick: () => { li.remove(); updateAdd(); }, "aria-label": "Remove this line" }, "Remove"));
    mustList.append(li);
    updateAdd();
  };
  const addBtn = h("button", { type: "button", class: "btn small", onclick: () => { addMust(); /** @type {HTMLInputElement} */ (mustList.lastElementChild?.querySelector("input"))?.focus(); } }, "Add a line");
  const updateAdd = () => { addBtn.disabled = mustList.children.length >= 5; };
  addMust();

  // references
  /** @type {{file: File, url: string}[]} */
  const refs = [];
  const refGrid = h("ul", { class: "ref-grid" });
  const moodBanner = h("p", { class: "note warn", role: "status", hidden: true }, "Using a picture as a mood image can make the result look very close to it. We recommend turning on “Check similarity to my reference” below.");
  const similarity = switchField({ label: "Check similarity to my reference", hint: "Off by default. When on, the finished duo is compared with your picture and you are told if it looks too close." });
  const mood = switchField({ label: "Use my picture as a mood image", hint: "Off by default. The picture is only used for ideas, never copied.", onChange: (on) => { moodBanner.hidden = !on; } });
  const paintRefs = () => {
    setChildren(refGrid, ...refs.map((r, i) => h("li", {}, h("img", { src: r.url, alt: `Reference picture ${i + 1}: ${r.file.name}` }), h("span", { class: "ref-name" }, r.file.name),
      h("button", { type: "button", class: "btn quiet small", onclick: () => { URL.revokeObjectURL(r.url); refs.splice(i, 1); paintRefs(); } }, "Remove"))));
  };
  const dz = dropzone({ accept: [".png", ".jpg", ".jpeg", ".webp"], maxBytes: 50 * 1024 * 1024, multiple: true, title: "Drop up to 4 reference pictures here, or press to choose", hint: "PNG, JPEG or WebP. These stay on your computer except where a service needs to see them.",
    onFiles: (files) => {
      for (const f of files) {
        if (refs.length >= MAX_REFS) { dz.setStatus("A duo can have at most 4 reference pictures.", "bad"); break; }
        refs.push({ file: f, url: URL.createObjectURL(f) });
      }
      paintRefs();
    } });
  ctx.onCleanup(() => refs.forEach((r) => URL.revokeObjectURL(r.url)));

  const budget = h("input", { type: "number", min: "1", step: "0.5", value: String(defaults?.budgets?.per_duo_usd ?? 15), class: "narrow" });
  const ask = h("input", { type: "number", min: "0", step: "0.5", value: String(defaults?.budgets?.ask_above_usd ?? 2), class: "narrow" });
  const mesh = h("fieldset", { class: "choice-group" }, h("legend", {}, "3D parts (hair and accessories)"),
    [["ask", "Ask me when that step starts (recommended)", "You see the estimate first."], ["api", "Let Tripo make them automatically", "About $1.10 per try, charged to your Tripo credits."], ["manual", "I will make them myself on Tripo's website", "We prepare a pack; you bring the file back."]].map(([v, t, sub]) => {
      const id = uid("mesh");
      return h("label", { class: "choice", for: id }, h("input", { type: "radio", name: "mesh", id, value: v, checked: v === (defaults?.three_d?.default_mesh_mode ?? "ask") }), h("span", {}, h("strong", {}, t), h("br"), h("small", { class: "muted" }, sub)));
    }));

  const err = h("div", { class: "msg" });
  const start = h("button", { type: "submit", class: "btn primary big" }, "Start the plan");
  const form = h("form", { class: "brief-form", novalidate: true });
  /** @param {string} label @param {HTMLElement} control @param {string} [hint] */
  const f = (label, control, hint) => field(label, control, hint);
  form.append(
    panel({ title: "1. The idea" },
      f("Name this duo", name, "Only you see this name."),
      combos,
      f("What is the idea?", brief, "A few sentences is plenty. You can leave it empty and let the planner surprise you."), h("div", { class: "hint-row" }, briefCount),
      f("How should the two relate?", structure, "“Let the planner choose” gives you three different ideas to pick from.")),
    panel({ title: "2. Things that must appear", lead: "Up to 5 short lines (12 words each). The planner has to include every one." }, mustList, h("div", { class: "row" }, addBtn)),
    panel({ title: "3. Pictures for inspiration (optional)", lead: "Up to 4. Skip this if you have none." }, dz, refGrid, mood, moodBanner, similarity),
    panel({ title: "4. Money and 3D" },
      h("div", { class: "grid-2" }, f("Budget cap for this duo ($)", budget, "DuoSkin stops and asks before going past this."), f("Ask me before any step over ($)", ask, "A single step costing more than this always waits for your yes.")), mesh),
    h("section", { class: "panel start-panel" },
      h("p", {}, demo ? h("strong", {}, "Demo mode is on: practice pictures, nothing is charged.") : [h("strong", {}, "Planning and the first concept pictures usually cost about $2 to $4.50"), " (an estimate). You see three plans and approve one before anything bigger runs."]),
      h("p", { class: "muted" }, "Nothing is charged until you press the button, and nothing expensive runs without your approval."),
      err, h("div", { class: "row" }, start, h("a", { class: "btn", href: "#/" }, "Cancel"))));

  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    setChildren(err);
    const words = (/** @type {string} */ s) => s.trim().split(/\s+/).filter(Boolean).length;
    const lines = Array.from(mustList.querySelectorAll("input")).map((i) => i.value.trim()).filter(Boolean);
    if (!name.value.trim()) { err.append(note("Please give the duo a name.", "bad")); name.focus(); return; }
    if (lines.some((l) => words(l) > 12)) { err.append(note("Each must-include line can have at most 12 words.", "bad")); return; }
    const cap = Number(budget.value);
    if (!(cap > 0)) { err.append(note("The budget cap must be more than zero.", "bad")); budget.focus(); return; }
    start.disabled = true;
    start.setAttribute("aria-busy", "true");
    /** @type {any} */ let project = null;
    try {
      const combo = /** @type {HTMLInputElement} */ (form.querySelector('input[name="combo"]:checked')).value;
      const meshMode = /** @type {HTMLInputElement} */ (form.querySelector('input[name="mesh"]:checked')).value;
      project = await post("/api/projects", {
        name: name.value.trim(), combo, brief: brief.value.trim(), structure_request: structure.value, must_include: lines,
        settings: { budget_usd: cap, ask_above_usd: Number(ask.value) || 0, mesh_mode: meshMode, use_reference_as_mood: mood.input.checked && refs.length > 0, reference_similarity_check: similarity.input.checked },
      });
      for (const r of refs) {
        const fd = new FormData();
        fd.append("file", r.file, r.file.name);
        fd.append("role", "reference");
        await post(`/api/projects/${project.id}/references`, fd);
      }
      try {
        await post(`/api/projects/${project.id}/plan`);
      } catch (e2) {
        if (e2 instanceof ApiError && e2.unavailable) {
          ctx.navigate(`/p/${project.id}`);
          return;
        }
        throw e2;
      }
      ctx.navigate(`/p/${project.id}/plan`);
    } catch (e3) {
      const msg = friendly(e3);
      setChildren(err, note(project ? `Your duo “${project.name}” was saved, but: ${msg}` : msg, "bad"),
        project ? h("a", { class: "btn", href: `#/p/${project.id}` }, "Open the saved duo") : null);
      start.disabled = false;
      start.removeAttribute("aria-busy");
    }
  });

  setChildren(ctx.root, pageHeader({ title: "New duo", lead: "Tell us the idea. Nothing is made yet: you will get three plans to choose from.", back: { href: "#/", label: "Your duos" } }), form);
}
