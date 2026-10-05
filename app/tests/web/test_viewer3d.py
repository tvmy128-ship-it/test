"""The 3D viewer under the strict Content-Security-Policy: vendored three.js, an embedded texture, GLB and the fallbacks."""
from __future__ import annotations

import base64
import io
from pathlib import Path

import pytest
from PIL import Image
from playwright.sync_api import expect

from duoskin.engine.cas import make_prov

MESHES = Path(__file__).resolve().parents[2] / "duoskin" / "providers" / "fixtures" / "meshes"

MOUNT = """async ({url, height}) => {
    const { createViewer } = await import('/web/components/viewer3d.js');
    const host = document.createElement('div');
    host.id = 'test-viewer';
    document.body.append(host);
    const v = createViewer(host, { height });
    window.__viewer = v;
    try { await v.load([{ url }]); } catch (e) { return { error: String(e) }; }
    return { ready: v.ready, stats: host.querySelector('.viewer-stats').textContent, status: host.querySelector('.viewer-status').textContent };
}"""


def webgl_ok(page) -> bool:
    return page.evaluate("(() => { try { const c = document.createElement('canvas'); return !!(c.getContext('webgl2') || c.getContext('webgl')); } catch (e) { return false; } })()")


def rendered_pixels(page) -> int:
    """How many pixels of the viewer's canvas are not fully transparent (it really drew the model)."""
    data = page.evaluate("window.__viewer.snapshot()")
    im = Image.open(io.BytesIO(base64.b64decode(data.split(",", 1)[1]))).convert("RGBA")
    return sum(1 for a in im.getchannel("A").getdata() if a > 0)


def test_glb_with_an_embedded_texture_renders_with_no_csp_violation(ui, live):
    sha = live.rt.cas.put((MESHES / "plush_pet.glb").read_bytes(), "glb", prov=make_prov("mock")).sha256
    ui.goto("/")
    if not webgl_ok(ui.page):
        pytest.skip("this Chromium has no WebGL")
    res = ui.page.evaluate(MOUNT, {"url": f"/cas/{sha}.glb", "height": 320})
    assert res.get("ready") is True and "error" not in res, res
    assert "2,280" in res["stats"] or "2280" in res["stats"]                     # triangles counted from the file
    ui.page.wait_for_timeout(600)
    assert rendered_pixels(ui.page) > 2000                                          # the model, with its texture, was drawn
    ui.shot_element(ui.page.locator("#test-viewer"), "viewer3d_plush_pet")
    ui.no_errors()                                                                  # incl. "Refused to connect to blob:" style CSP errors


def test_viewer_fallback_for_a_broken_file_is_a_plain_message(ui, live):
    sha = live.rt.cas.put(b"this is not a 3d model at all", "bin", prov=make_prov("mock")).sha256
    ui.goto("/")
    res = ui.page.evaluate(MOUNT.replace("kind", "kind"), {"url": f"/cas/{sha}.bin", "height": 200})
    assert "error" in res
    expect(ui.page.locator("#test-viewer .viewer-status")).to_contain_text("could not be opened")
    assert "Error" not in ui.page.locator("#test-viewer .viewer-status").inner_text()


def test_vendored_three_is_served_from_this_origin_only(ui, live):
    seen: list[str] = []
    ui.page.on("request", lambda r: seen.append(r.url))
    sha = live.rt.cas.put((MESHES / "icosphere_2k.glb").read_bytes(), "glb", prov=make_prov("mock")).sha256
    ui.goto("/")
    if not webgl_ok(ui.page):
        pytest.skip("this Chromium has no WebGL")
    ui.page.evaluate(MOUNT, {"url": f"/cas/{sha}.glb", "height": 240})
    external = [u for u in seen if not u.startswith(live.url) and not u.startswith(("data:", "blob:"))]
    assert not external, f"requests left this origin: {external}"
    assert any("/vendor/three/build/three.module.js" in u for u in seen) and any("/vendor/three/addons/loaders/GLTFLoader.js" in u for u in seen)


def test_three_is_loaded_only_when_a_viewer_is_opened(ui, live):
    seen: list[str] = []
    ui.page.on("request", lambda r: seen.append(r.url))
    ui.goto("/")
    ui.goto("/settings")
    assert not any("three" in u for u in seen), "the home and settings pages must not pull in 1.4 MB of 3D code"


def test_viewer_controls_turntable_and_reset(ui, live):
    sha = live.rt.cas.put((MESHES / "rounded_box_2k.glb").read_bytes(), "glb", prov=make_prov("mock")).sha256
    ui.goto("/")
    if not webgl_ok(ui.page):
        pytest.skip("this Chromium has no WebGL")
    ui.page.evaluate("""async (url) => {
        const { viewerPanel } = await import('/web/components/viewer3d.js');
        const { el } = viewerPanel({ models: [{ url }], height: 260 });
        el.id = 'panel';
        document.body.append(el);
    }""", f"/cas/{sha}.glb")
    panel = ui.page.locator("#panel")
    expect(panel.locator(".viewer-stats")).to_contain_text("triangles", timeout=15000)
    btn = panel.get_by_role("button", name="Stop turning")
    assert btn.get_attribute("aria-pressed") == "true"
    btn.click()
    expect(panel.get_by_role("button", name="Turn slowly")).to_have_attribute("aria-pressed", "false")
    panel.get_by_role("button", name="Reset view").click()
    assert panel.locator("canvas").count() == 1
    ui.no_errors()
