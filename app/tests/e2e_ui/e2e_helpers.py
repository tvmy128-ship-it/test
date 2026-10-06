"""Helpers shared by the browser end-to-end tests: the API as the page calls it, and the scan for what a person must never see."""
from __future__ import annotations

import re

import httpx

JUNK = [
    (re.compile(r"Traceback|\bFile \"|\.py\b|\bat 0x"), "a stack trace"),
    (re.compile(r"[{]\s*\""), "raw JSON"),
    (re.compile(r"\b(prj|spc|gat|job|stp|chg|dec|ast|pls|cli)_[0-9a-z]{16,}\b"), "an internal id"),
    (re.compile(r"\b[0-9a-f]{40,64}\b"), "a content hash"),
    (re.compile(r"\bundefined\b|\[object |\bNaN\b|\bnull\b"), "a JavaScript leftover"),
    (re.compile(r"\b(hard_failures|HARD|A_LEAK|A_PALETTE|F_LINE_SKIN|CHK-[A-Z]\d+)\b"), "a check code"),
]


class Api:
    """The app's HTTP API, as the page itself calls it (the per-launch token goes in every request)."""

    def __init__(self, live) -> None:
        self.c = httpx.Client(base_url=live.url, headers={"X-DuoSkin-Token": live.rt.token}, timeout=60)

    def get(self, path: str, **params):
        r = self.c.get(path, params=params)
        assert r.status_code == 200, f"GET {path}: {r.status_code} {r.text[:300]}"
        return r.json()

    def post(self, path: str, body=None, expect_status: int = 200):
        r = self.c.post(path, json=body)
        assert r.status_code == expect_status, f"POST {path}: {r.status_code} {r.text[:300]}"
        return r.json() if r.content else None

    def project(self, pid: str):
        return self.get(f"/api/projects/{pid}")

    def gates(self, pid: str, kind: str | None = None):
        gs = self.get("/api/gates", project_id=pid, state="open")
        return [g for g in gs if kind is None or g["kind"] == kind]

    def gate(self, pid: str, kind: str):
        found = self.gates(pid, kind)
        return found[0] if found else None

    def tile(self, pid: str, kind: str, tile_id: str):
        g = self.gate(pid, kind)
        return next((t for t in g["tiles"] if t["tile_id"] == tile_id), None) if g else None


def clean(ui, where: str, selector: str = "body") -> None:
    """The page says nothing a person must never see."""
    text = ui.page.locator(selector).inner_text()
    for rx, what in JUNK:
        m = rx.search(text)
        assert m is None, f"{where}: the page shows {what}: ...{text[max(0, m.start() - 60):m.end() + 60]!r}..."


def stage_is(api: Api, pid: str, *stages: str) -> bool:
    return api.project(pid)["project"]["stage"] in stages


def dismiss_toasts(ui) -> None:
    ui.page.evaluate("document.querySelectorAll('.toast').forEach(t => t.remove())")


def dialog(ui):
    return ui.page.locator("dialog[open]").last
