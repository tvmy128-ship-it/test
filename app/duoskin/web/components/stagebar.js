// @ts-check
// The stage bar: Brief > Plan > Concept > Parts > Build > Duo > Export, always visible while a duo is open.
import { h, comboLabel, setChildren } from "../dom.js";
import { STAGES, stageHref, stageIndex, stageSentence } from "../text.js";

/**
 * @param {HTMLElement} host
 * @param {{id: string, name: string, stage: string, combo?: string} | null} project
 * @param {number} waiting how many decisions are waiting on the user
 */
export function renderStageBar(host, project, waiting = 0) {
  if (!project) {
    setChildren(host, h("p", { class: "stagebar-idle" }, "Nothing costs money until you approve it. What you spend shows here."));
    host.classList.add("idle");
    return;
  }
  host.classList.remove("idle");
  const current = stageIndex(project.stage);
  const finished = project.stage === "exported";
  const items = STAGES.map((s, i) => {
    const done = i < current || (finished && i === current);
    const isCurrent = i === current && !finished;
    const cls = ["stage", done ? "done" : "", isCurrent ? "current" : "", i > current ? "todo" : ""].filter(Boolean).join(" ");
    const mark = done ? "✓" : String(i + 1);
    const inner = [h("span", { class: "stage-mark", "aria-hidden": "true" }, mark), h("span", { class: "stage-name" }, s.label),
      done ? h("span", { class: "sr" }, " (done)") : null,
      isCurrent && waiting ? h("span", { class: "stage-turn" }, "Your turn") : null];
    const node = i <= current
      ? h("a", { href: stageHref(project.id, i), class: "stage-link", "aria-current": isCurrent ? "step" : null, title: s.hint }, inner)
      : h("span", { class: "stage-link disabled", title: s.hint }, inner);
    return h("li", { class: cls }, node);
  });
  setChildren(host, 
    h("a", { class: "stage-project", href: `#/p/${project.id}`, title: "Open this duo's page" }, h("strong", {}, project.name), h("span", {}, project.combo ? comboLabel(project.combo) : "")),
    h("nav", { class: "stage-nav", "aria-label": `Progress of ${project.name}` }, h("ol", { class: "stagebar" }, items)),
    h("p", { class: "sr", role: "status" }, `${project.name}: ${stageSentence(project.stage)}`));
}
