"""``tools/vendor_three.py`` and the vendored three.js under duoskin/web/vendor/three (no network, no browser)."""
from __future__ import annotations

import importlib.util
import json
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
VENDOR = ROOT / "duoskin" / "web" / "vendor" / "three"
TOOL = ROOT / "tools" / "vendor_three.py"
TARBALL_HINTS = sorted(Path("/tmp").glob("claude-*/**/scratchpad/three.tgz"))


def load_tool():
    spec = importlib.util.spec_from_file_location("vendor_three", TOOL)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["vendor_three"] = mod
    spec.loader.exec_module(mod)
    return mod


def test_the_vendored_files_match_their_manifest():
    tool = load_tool()
    assert tool.check(VENDOR) == []
    manifest = json.loads((VENDOR / "VENDOR.json").read_text(encoding="utf-8"))
    assert manifest["package"] == "three" and manifest["license"] == "MIT" and manifest["version"] == tool.VERSION
    assert manifest["tarball_sha256"] == tool.TARBALL_SHA256
    for needed in ("build/three.module.js", "build/three.core.js", "addons/loaders/GLTFLoader.js", "addons/controls/OrbitControls.js",
                   "addons/environments/RoomEnvironment.js", "addons/loaders/FBXLoader.js", "LICENSE"):
        assert needed in manifest["files"], needed


def test_an_edited_file_is_detected(tmp_path):
    tool = load_tool()
    copy = tmp_path / "three"
    import shutil

    shutil.copytree(VENDOR, copy)
    (copy / "build" / "three.core.js").write_text("// tampered", encoding="utf-8")
    (copy / "addons" / "extra.js").write_text("// unexpected", encoding="utf-8")
    problems = tool.check(copy)
    assert "changed build/three.core.js" in problems and "unexpected addons/extra.js" in problems


def test_every_relative_import_of_the_vendored_addons_is_vendored():
    """No add-on may import a file that was left out (that would be a 404 in the browser)."""
    tool = load_tool()
    missing = []
    for js in (VENDOR / "addons").rglob("*.js"):
        text = js.read_text(encoding="utf-8")
        for spec in tool.relative_imports(text):
            target = (js.parent / spec).resolve()
            if not target.exists():
                missing.append(f"{js.relative_to(VENDOR)} -> {spec}")
    assert not missing, missing
    core = (VENDOR / "build" / "three.module.js").read_text(encoding="utf-8")
    assert "./three.core.js" in core and (VENDOR / "build" / "three.core.js").exists()


def test_import_resolution_and_the_pinned_tarball_check():
    tool = load_tool()
    src = "import { A } from 'three';\nimport { b } from '../utils/B.js';\nexport { c } from './c.js';\nimport './side.js';\nconst x = 1; // import('./not-static.js')"
    assert tool.relative_imports(src) == ["../utils/B.js", "./c.js", "./side.js"]
    assert tool._normalise("examples/jsm/loaders/GLTFLoader.js", "../utils/B.js") == "examples/jsm/utils/B.js"
    with pytest.raises(tool.VendorError):
        tool._normalise("a.js", "../../escape.js")


@pytest.mark.skipif(not TARBALL_HINTS, reason="the three-0.186.1 tarball is not on this machine")
def test_vendoring_again_is_reproducible(tmp_path):
    tool = load_tool()
    out = tmp_path / "three"
    manifest = tool.vendor(TARBALL_HINTS[0], out)
    assert manifest["files"] == json.loads((VENDOR / "VENDOR.json").read_text(encoding="utf-8"))["files"]
    assert tool.check(out) == []


def test_the_page_import_map_points_at_the_vendored_files_and_matches_the_csp_hash():
    html = (ROOT / "duoskin" / "web" / "index.html").read_text(encoding="utf-8")
    m = re.search(r'<script type="importmap">(.*?)</script>', html, re.DOTALL)
    imports = json.loads(m.group(1))["imports"]
    assert imports["three"] == "/vendor/three/build/three.module.js" and imports["three/addons/"] == "/vendor/three/addons/"
    assert (VENDOR / "build" / "three.module.js").exists()
    from duoskin import security

    sha = security.importmap_csp_source(html)
    assert sha and sha in security.content_security_policy(sha)
