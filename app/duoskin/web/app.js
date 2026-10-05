// DuoSkin Studio: minimal shell page (health, projects, jobs, keys, settings, doctor).
// The real UI is another track's job; it replaces this file. No inline scripts or styles (CSP).
const token = document.querySelector('meta[name="duoskin-token"]')?.content || "";
const $ = (id) => document.getElementById(id);

function h(tag, attrs = {}, ...kids) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "class") el.className = v;
    else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else if (v === true) el.setAttribute(k, "");
    else if (v !== false && v != null) el.setAttribute(k, v);
  }
  for (const kid of kids.flat()) el.append(kid instanceof Node ? kid : document.createTextNode(String(kid ?? "")));
  return el;
}

async function api(method, path, body) {
  const opts = { method, headers: {} };
  if (method !== "GET") opts.headers["X-DuoSkin-Token"] = token;
  if (body !== undefined) { opts.headers["Content-Type"] = "application/json"; opts.body = JSON.stringify(body); }
  const res = await fetch(path, opts);
  if (res.status === 403) { const j = await res.json().catch(() => ({})); if (j.error === "bad_token") { location.reload(); } }
  const text = await res.text();
  let data = null;
  try { data = text ? JSON.parse(text) : null; } catch { data = { message: text }; }
  if (!res.ok) throw Object.assign(new Error(data?.message || data?.error || res.statusText), { status: res.status, data });
  return data;
}

function section(id, ...kids) {
  const sec = $(id);
  const heading = sec.querySelector("h2");
  sec.replaceChildren(heading, ...kids.flat());
}
const money = (n) => "$" + Number(n || 0).toFixed(2);

async function refreshHealth() {
  const pill = $("health");
  try {
    const hd = await api("GET", "/api/health");
    pill.textContent = hd.ok ? `ready, v${hd.version}` : "database problem";
    pill.className = "pill " + (hd.ok ? "ok" : "bad");
    $("banner").classList.toggle("hidden", !hd.demo);
    $("banner").textContent = hd.demo ? "DEMO: every provider is a mock. Nothing here can be exported." : "";
  } catch (e) {
    pill.textContent = "offline"; pill.className = "pill bad";
  }
}

async function refreshState() {
  const st = await api("GET", "/api/state");
  const q = $("queue");
  const parts = [];
  if (st.queue.paused) parts.push("paused: " + st.queue.paused);
  if (st.queue.paid_blocked) parts.push("paid steps blocked");
  const waiting = st.open_gates.length;
  if (waiting) parts.push(`${waiting} waiting on you`);
  q.textContent = parts.join(" | ");
  q.className = "pill warn" + (parts.length ? "" : " hidden");
  renderProjects(st.projects);
  renderJobs(st.jobs);
}

function renderProjects(projects) {
  const form = h("div", { class: "row" },
    h("input", { id: "np-name", type: "text", placeholder: "Name", "aria-label": "Project name", maxlength: "60" }),
    h("select", { id: "np-combo", "aria-label": "Pair" }, ...[["bg", "boy + girl"], ["gb", "girl + boy"], ["bb", "boy + boy"], ["gg", "girl + girl"]].map(([v, t]) => h("option", { value: v }, t))),
    h("button", { type: "button", class: "primary", onclick: async () => {
      const name = $("np-name").value.trim();
      if (!name) return;
      await api("POST", "/api/projects", { name, combo: $("np-combo").value, brief: "" });
      await refreshState();
    } }, "New duo"));
  const rows = projects.map((p) => h("tr", {}, h("td", {}, p.name), h("td", {}, p.combo), h("td", {}, p.stage), h("td", {}, money(p.spent_usd)), h("td", {}, p.waiting_on_user ? `${p.waiting_on_user} waiting on you` : "")));
  section("projects", form, projects.length
    ? h("div", { class: "wrap" }, h("table", {}, h("thead", {}, h("tr", {}, ...["Name", "Pair", "Stage", "Spent", ""].map((t) => h("th", {}, t)))), h("tbody", {}, rows)))
    : h("p", { class: "muted" }, "No projects yet."));
}

function renderJobs(jobs) {
  if (!jobs.length) return section("jobs", h("p", { class: "muted" }, "Nothing has run yet."));
  const rows = jobs.map((j) => {
    const counts = Object.entries(j.steps_by_state).map(([k, v]) => `${v} ${k}`).join(", ");
    const cancel = ["succeeded", "failed", "cancelled"].includes(j.job.state) ? null
      : h("button", { type: "button", onclick: async () => { await api("POST", `/api/jobs/${j.job.id}/cancel`); await refreshState(); } }, "Cancel");
    const retry = j.steps_by_state.failed ? h("button", { type: "button", onclick: () => showJob(j.job.id) }, "Show failures") : null;
    return h("tr", {}, h("td", {}, j.job.kind), h("td", { class: j.job.state === "failed" ? "bad" : "" }, j.job.state), h("td", {}, counts), h("td", {}, h("span", { class: "row" }, cancel, retry)));
  });
  section("jobs", h("div", { class: "wrap" }, h("table", {}, h("thead", {}, h("tr", {}, ...["Job", "State", "Steps", ""].map((t) => h("th", {}, t)))), h("tbody", {}, rows))), h("div", { id: "job-detail" }));
}

async function showJob(id) {
  const j = await api("GET", `/api/jobs/${id}`);
  const rows = j.steps.map((s) => h("tr", {}, h("td", {}, s.kind), h("td", {}, s.state), h("td", { class: s.error ? "bad" : "muted" }, s.error?.user_hint || s.error?.message || s.message),
    h("td", {}, s.state === "failed" ? h("button", { type: "button", onclick: async () => { await api("POST", `/api/steps/${s.id}/retry`); await refreshState(); await showJob(id); } }, "Retry") : "")));
  $("job-detail").replaceChildren(h("h3", {}, `Steps of ${j.job.kind}`), h("div", { class: "wrap" }, h("table", {}, h("tbody", {}, rows))));
}

async function renderKeys() {
  const keys = await api("GET", "/api/keys");
  const rows = Object.values(keys).map((k) => {
    const input = h("input", { type: "password", autocomplete: "off", placeholder: k.set ? "replace key" : "paste key", "aria-label": `${k.provider} key` });
    const note = h("span", { class: "small muted" }, k.test_cost_note || "");
    const result = h("span", { class: "small" }, k.last_test ? (k.last_test.ok ? "test ok" : "test: " + k.last_test.message) : "");
    return h("tr", {}, h("td", {}, k.provider), h("td", {}, k.requirement),
      h("td", { class: k.set ? "ok" : "muted" }, k.set ? `${k.masked} (${k.source === "environment" ? "from environment" : "saved"})` : "not set"),
      h("td", {}, h("div", { class: "row" }, input,
        h("button", { type: "button", onclick: async () => { if (!input.value) return; try { await api("PUT", `/api/keys/${k.provider}`, { value: input.value }); input.value = ""; await renderKeys(); } catch (e) { result.textContent = e.message; result.className = "small bad"; } } }, "Save"),
        h("button", { type: "button", onclick: async () => { result.textContent = "testing..."; const r = await api("POST", `/api/keys/${k.provider}/test`); result.textContent = r.ok === null ? r.message : (r.ok ? "test ok" : "test failed: " + r.message); result.className = "small " + (r.ok ? "ok" : "bad"); } }, "Test key"),
        k.set && k.source !== "environment" ? h("button", { type: "button", class: "danger", onclick: async () => { await api("DELETE", `/api/keys/${k.provider}`); await renderKeys(); } }, "Remove") : null, note, result)));
  });
  section("keys", h("p", { class: "small muted" }, "Keys are stored in Windows Credential Manager and never shown again."),
    h("div", { class: "wrap" }, h("table", {}, h("thead", {}, h("tr", {}, ...["Provider", "Needed", "Status", ""].map((t) => h("th", {}, t)))), h("tbody", {}, rows))));
}

async function renderSettings() {
  const s = await api("GET", "/api/settings");
  const save = async (patch) => { await api("PUT", "/api/settings", patch); await renderSettings(); await refreshHealth(); };
  const demo = h("input", { type: "checkbox", id: "set-demo", checked: s.demo_mode });
  demo.addEventListener("change", () => save({ demo_mode: demo.checked }));
  const cap = h("input", { type: "number", min: "1", step: "0.5", value: s.budgets.per_duo_usd, "aria-label": "Budget cap per duo (USD)" });
  const ask = h("input", { type: "number", min: "0", step: "0.5", value: s.budgets.ask_above_usd, "aria-label": "Ask above (USD)" });
  const modes = Object.entries(s.providers.modes).map(([p, m]) => {
    const sel = h("select", { "aria-label": `${p} mode` }, ...["real", "mock", "disabled"].map((v) => h("option", { value: v, selected: v === m }, v)));
    sel.addEventListener("change", () => save({ providers: { modes: { [p]: sel.value } } }));
    return h("div", { class: "row" }, h("span", {}, p), sel);
  });
  section("settings",
    h("div", { class: "row" }, demo, h("label", { for: "set-demo" }, "Demo mode (no keys needed; every provider is a mock)")),
    h("div", { class: "row" }, h("label", {}, "Budget cap per duo (USD) "), cap, h("label", {}, "Ask above (USD) "), ask,
      h("button", { type: "button", onclick: () => save({ budgets: { per_duo_usd: Number(cap.value), ask_above_usd: Number(ask.value) } }) }, "Save budgets")),
    h("h3", {}, "Providers"), ...modes);
}

async function renderDoctor() {
  const d = await api("GET", "/api/doctor");
  const run = h("button", { type: "button", class: "primary", onclick: async () => { run.disabled = true; await api("POST", "/api/doctor/run"); } }, d.running ? "Running..." : "Run doctor");
  if (d.running) run.disabled = true;
  if (!d.ran) return section("doctor", h("div", { class: "row" }, run), h("p", { class: "muted" }, "The doctor has not run yet."));
  const cls = { pass: "ok", warn: "warn", fail: "bad", na: "muted" };
  const rows = d.checks.map((c) => h("tr", {}, h("td", { class: cls[c.status] }, c.status), h("td", {}, c.id), h("td", {}, c.message, c.fix && (c.status === "fail" || c.status === "warn") ? h("div", { class: "small muted" }, "What to do: " + c.fix) : "")));
  section("doctor", h("div", { class: "row" }, run, h("span", { class: "small " + (d.blocks_paid_features ? "bad" : "muted") }, d.blocks_paid_features ? "Paid features are blocked until the failures are fixed." : `${d.summary.passed} passed, ${d.summary.warnings} warnings`)),
    h("div", { class: "wrap" }, h("table", {}, h("tbody", {}, rows))));
}

let timer = null;
function scheduleRefresh() {
  clearTimeout(timer);
  timer = setTimeout(() => { refreshState().catch(() => {}); }, 300);
}

function listen() {
  if (!window.EventSource) return;
  const es = new EventSource("/api/events");
  es.onmessage = (m) => {
    const ev = JSON.parse(m.data);
    if (ev.type === "doctor.result") renderDoctor();
    else if (["step.state", "job.state", "gate.opened", "gate.updated", "project.stage", "cost.added"].includes(ev.type)) scheduleRefresh();
  };
}

$("quit").addEventListener("click", async () => {
  if (!confirm("Quit DuoSkin Studio?")) return;
  try { await api("POST", "/api/shutdown"); } catch { /* the server is going away */ }
  document.body.textContent = "DuoSkin Studio has stopped. You can close this tab.";
});

await refreshHealth();
await Promise.all([refreshState(), renderKeys(), renderSettings(), renderDoctor()]);
listen();
setInterval(refreshHealth, 15000);
