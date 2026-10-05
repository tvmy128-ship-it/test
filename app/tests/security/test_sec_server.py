"""Security lens 3: the local server, attacked the way a malicious web page, a DNS-rebinding site or a local process would.

Complements ``tests/api/test_api_security.py`` (token, Origin, Host, CORS, CSP, CAS sandbox) with: cross-site fetch metadata, resource
policy headers, token leakage, Windows path tricks against ``/web``, ``/vendor`` and ``/cas``, symlinks, the open-folder endpoint and the
shutdown endpoint.
"""
from __future__ import annotations

import ast
import hashlib
import os
from pathlib import Path

import pytest

from duoskin.engine.cas import make_prov
from duoskin.engine.testkit import make_app, make_client, png_bytes

APP_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def app(tmp_path):
    return make_app(tmp_path / "home", providers_mode="mock")


@pytest.fixture
def client(app):
    with make_client(app) as c:
        yield c


@pytest.fixture
def rt(client, app):
    return app.state.rt


@pytest.fixture
def anon(app):
    return make_client(app, authed=False)


# ------------------------------------------------------------------------------------------------- a malicious web page
@pytest.mark.parametrize("site", ["cross-site", "same-site"])
@pytest.mark.parametrize("method,path", [("GET", "/api/state"), ("GET", "/api/health"), ("GET", "/api/events?once=1"), ("GET", "/api/events/poll"),
                                         ("GET", "/api/keys"), ("GET", "/api/settings"), ("GET", "/cas/" + "a" * 64 + ".png"), ("POST", "/api/shutdown")])
def test_a_request_the_browser_marks_cross_site_is_not_served(client, method, path, site):
    r = client.request(method, path, headers={"Sec-Fetch-Site": site, "Sec-Fetch-Mode": "no-cors"})
    assert r.status_code == 403 and r.json() == {"error": "cross_site"}


@pytest.mark.parametrize("site", ["same-origin", "none"])
def test_our_own_page_and_a_typed_url_still_work(client, site):
    assert client.get("/api/state", headers={"Sec-Fetch-Site": site}).status_code == 200
    assert client.get("/", headers={"Sec-Fetch-Site": "cross-site"}).status_code == 200            # a link click to the page is a navigation, not an API call


def test_no_other_origin_can_embed_or_probe_our_files(client):
    for path in ("/", "/api/health", "/web/app.js", "/web/css/components.css"):
        r = client.get(path)
        assert r.headers["cross-origin-resource-policy"] == "same-origin", path
    html = client.get("/")
    assert html.headers["x-frame-options"] == "DENY" and html.headers["cross-origin-opener-policy"] == "same-origin"
    assert "frame-ancestors 'none'" in html.headers["content-security-policy"]


def test_the_launch_token_is_only_in_the_shell_page(client, app):
    token = app.state.rt.token
    assert token in client.get("/").text
    for path in ("/api/health", "/api/state", "/api/settings", "/api/doctor", "/api/keys", "/api/jobs", "/api/events/poll", "/api/events?once=1",
                 "/web/app.js", "/web/api.js", "/web/index.html", "/api/inbox", "/api/queue"):
        r = client.get(path)
        assert token not in r.text, path
        assert token not in "".join(f"{k}: {v}" for k, v in r.headers.items()), path


def test_the_token_never_travels_in_a_url_or_cookie_and_the_page_sends_no_referrer(client):
    r = client.get("/")
    assert r.headers["referrer-policy"] == "no-referrer" and "set-cookie" not in r.headers
    js = "\n".join(p.read_text(encoding="utf-8") for p in (APP_ROOT / "duoskin" / "web").rglob("*.js") if "vendor" not in p.parts)
    assert "token=" not in js.lower().replace("renderToken", "") or "X-DuoSkin-Token" in js
    for needle in ("?token", "&token", "localStorage.setItem(\"token", "document.cookie"):
        assert needle not in js


def test_the_token_is_not_written_to_the_logs(app, tmp_path):
    from duoskin import logsetup

    handle = logsetup.setup_logging(tmp_path / "logs", console=False, hooks=False)
    try:
        with make_client(app) as c:
            token = app.state.rt.token
            c.get("/")
            c.post("/api/doctor/run")
            c.post("/api/shutdown", headers={"X-DuoSkin-Token": "wrong-" + token})
            c.post("/api/keys/openai", headers={"X-DuoSkin-Token": "wrong"})
    finally:
        handle.shutdown()
    text = "".join(f.read_text(encoding="utf-8", errors="replace") for f in (tmp_path / "logs").glob("*"))
    assert token not in text


def test_a_wrong_or_empty_token_is_refused_in_constant_form(anon, app):
    token = app.state.rt.token
    for bad in ("", " ", token[:-1], token + "x", token.upper(), token[::-1], "null", "undefined"):
        r = anon.post("/api/doctor/run", headers={"X-DuoSkin-Token": bad})
        assert r.status_code == 403 and r.json() == {"error": "bad_token"}, bad
    assert anon.post("/api/doctor/run", headers={"X-DuoSkin-Token": token}).status_code == 202


@pytest.mark.parametrize("host", ["127.0.0.1.evil.com", "evil.com", "127.0.0.1@evil.com", "localhost:8765@evil.com", "[::1]:8765", "0.0.0.0:8765",
                                  "localhost.", "127.1", "2130706433", "127.0.0.1:8765:evil", "LOCALHOST:8765", "xn--localhost", " 127.0.0.1", "127.0.0.1 ",
                                  "foo.localhost", "localhost.evil.com", "127.0.0.1\t"])
def test_dns_rebinding_hosts_never_reach_a_route(client, host):
    for path in ("/api/health", "/", "/api/events?once=1", "/web/app.js"):
        try:
            r = client.get(path, headers={"Host": host})
        except Exception as exc:  # noqa: BLE001 - the HTTP client itself may refuse a malformed header
            assert "header" in str(exc).lower() or "illegal" in str(exc).lower() or "invalid" in str(exc).lower(), exc
            continue
        assert r.status_code == 400, (host, path, r.status_code)


def test_no_endpoint_enables_cors_or_a_wildcard_policy(client):
    for method in ("GET", "POST", "PUT", "DELETE", "OPTIONS", "PATCH"):
        r = client.request(method, "/api/state", headers={"Origin": "http://evil.example", "Access-Control-Request-Method": "POST",
                                                          "Access-Control-Request-Headers": "x-duoskin-token"})
        assert not any(k.lower().startswith("access-control-") for k in r.headers), method
        assert r.status_code != 200 or method == "GET"


def test_the_docs_and_schema_pages_are_off_unless_dev_mode(client):
    for path in ("/api/docs", "/api/redoc", "/api/openapi.json", "/docs", "/openapi.json"):
        assert client.get(path).status_code == 404


# ------------------------------------------------------------------------------------------------- shutdown
def test_shutdown_needs_the_token_and_the_origin_and_calls_the_host_once(app, client, anon):
    rt = app.state.rt
    calls: list[int] = []
    rt.on_shutdown = lambda: calls.append(1)
    assert anon.post("/api/shutdown").status_code == 403
    assert client.post("/api/shutdown", headers={"Origin": "http://evil.example"}).status_code == 403
    assert client.post("/api/shutdown", headers={"Sec-Fetch-Site": "cross-site"}).status_code == 403
    assert client.get("/api/shutdown").status_code == 405
    assert calls == []
    assert client.post("/api/shutdown").status_code == 202 and calls == [1]


# ------------------------------------------------------------------------------------------------- static files and CAS
WIN_TRICKS = ["app.js::$DATA", "app.js%3a%3a%24DATA", "app.js.", "app.js%20", "app.js%2e", "APP~1.JS", "..%5c..%5cpyproject.toml", "%2e%2e%5c%2e%2e%5cpyproject.toml",
              "..\\..\\pyproject.toml", "C:\\Windows\\win.ini", "C%3a%5cWindows%5cwin.ini", "\\\\?\\C:\\Windows\\win.ini", "\\\\server\\share\\x", "%00", "app.js%00.png",
              "con", "nul.js", "aux.txt", "....//....//pyproject.toml", "..%252f..%252fpyproject.toml", "%c0%ae%c0%ae/%c0%ae%c0%ae/pyproject.toml",
              "pages/../../app.py", "pages/..%2f..%2f..%2fapp.py"]


@pytest.mark.parametrize("trick", WIN_TRICKS)
def test_static_paths_cannot_leave_the_web_folder_or_reach_a_stream_or_device(client, trick):
    for prefix in ("/web/", "/vendor/", "/web/vendor/three/"):
        r = client.get(prefix + trick)
        assert r.status_code in (400, 404), (prefix + trick, r.status_code)
        assert b"[project]" not in r.content and b"win.ini" not in r.content and b"def create_app" not in r.content


def test_a_symlink_in_the_web_folder_is_not_followed(tmp_path, monkeypatch):
    if os.name == "nt":
        pytest.skip("creating symlinks needs a privilege on Windows")
    from duoskin import app as app_mod

    secret = tmp_path / "secret.txt"
    secret.write_text("TOP-SECRET-FILE", encoding="utf-8")
    web = tmp_path / "web"
    web.mkdir()
    (web / "index.html").write_text("<html></html>", encoding="utf-8")
    (web / "link.txt").symlink_to(secret)
    (web / "linkdir").symlink_to(tmp_path, target_is_directory=True)
    monkeypatch.setattr(app_mod, "WEB_DIR", web)
    app = make_app(tmp_path / "home", providers_mode="mock")
    with make_client(app) as c:
        assert c.get("/web/link.txt").status_code == 404
        assert c.get("/web/linkdir/secret.txt").status_code == 404
        assert b"TOP-SECRET" not in c.get("/web/link.txt").content


def _png_asset(rt, tint: int = 0):
    return rt.cas.put(png_bytes(color=(tint, 1, 2, 255)), "png", prov=make_prov("code"))


CAS_TRICKS = [(".png::$DATA", 404), (".png%3a%3a%24DATA", 404), (".png.", 404), (".png%20", 404), (".png%2e", 404), (".PNG", 404), (".png/x", 404),
              (".png%5c..%5c", 404), (".png%00", 404), (".png%0d%0aSet-Cookie:%20x=1", 404), ("%2e%2e%2f", 404), ("/../../../etc/passwd", 404),
              ("..%2f..%2f", 404), (".png%2f..%2f..%2fetc%2fpasswd", 404), (".png%5c", 404),
              # these decode to the ordinary asset (a trailing slash redirects, a query string and a fragment are not part of the path)
              (".png/", 200), (".p%6eg", 200), ("%2epng", 200), (".png?x=1/../../", 200), (".png#frag/..", 200)]


@pytest.mark.parametrize("suffix,expected", CAS_TRICKS)
def test_cas_names_with_stream_dot_space_or_traversal_tricks_never_touch_another_file(client, rt, suffix, expected):
    asset = _png_asset(rt)
    r = client.get(f"/cas/{asset.sha256}{suffix}")
    assert r.status_code == expected, (suffix, r.status_code)
    if expected == 200:
        assert r.content == png_bytes(color=(0, 1, 2, 255))
    assert "set-cookie" not in r.headers


def test_cas_serves_only_what_the_database_knows_even_if_a_file_appears(client, rt):
    data = png_bytes(color=(9, 9, 9, 255))
    sha = hashlib.sha256(data).hexdigest()
    stray = rt.cas.root / sha[:2] / f"{sha}.png"
    stray.parent.mkdir(parents=True, exist_ok=True)
    stray.write_bytes(data)
    assert client.get(f"/cas/{sha}.png").status_code == 404


def test_every_cas_response_is_sandboxed_even_for_html_and_svg(client, rt):
    html = rt.cas.put(b"<html><script>alert(1)</script></html>", "html", prov=make_prov("code"))
    svg = rt.cas.put(b"<svg xmlns='http://www.w3.org/2000/svg' onload='alert(1)'/>", "svg", prov=make_prov("recraft"))
    for a, ext in ((html, "html"), (svg, "svg")):
        r = client.get(f"/cas/{a.sha256}.{ext}")
        assert r.headers["content-security-policy"] == "sandbox" and r.headers["x-content-type-options"] == "nosniff"
        assert r.headers["cross-origin-resource-policy"] == "same-origin"


# ------------------------------------------------------------------------------------------------- open folder
class Recorder:
    def __init__(self):
        self.opened: list[Path] = []

    def __call__(self, folder: Path) -> None:
        self.opened.append(folder)


@pytest.fixture
def opener(monkeypatch):
    from duoskin.api import os_open

    rec = Recorder()
    monkeypatch.setitem(os_open.OPENER, "fn", rec)
    return rec


def _open(client, kind, ident="", **extra):
    return client.post("/api/os/open-folder", json={"kind": kind, "id": ident, **extra})


def test_open_folder_opens_only_the_logs_inbox_pack_and_export_folders_of_the_app(client, rt, opener, tmp_path):
    exports = tmp_path / "exports"
    rt.update_settings({"paths": {"exports_root": str(exports), "tripo_inbox": str(exports / "inbox")}})
    (exports / "inbox").mkdir(parents=True)
    kit = exports / "Kit-1"
    kit.mkdir()
    pack = exports / "TripoPacks" / "p" / "DS-p-a-hair-0-abc123"
    pack.mkdir(parents=True)
    polish = exports / "PolishPacks" / "p" / "a-hair-0"
    polish.mkdir(parents=True)
    rt.repo.kv_set("export:prj_x", {"status": "done", "kit_dir": str(kit)})
    rt.repo.kv_set("pack:prj_x:a.hair", {"pack_id": "DS-p-a-hair-0-abc123", "folder": str(pack)})
    rt.repo.kv_set("pack:prj_x:a.hair.polish", {"pack_id": "DS-p-a-hair-0-polish", "folder": str(polish), "kind": "polish"})
    assert _open(client, "logs").status_code == 204 and opener.opened[-1] == rt.paths.logs_dir.resolve()
    assert _open(client, "inbox").status_code == 204 and opener.opened[-1] == (exports / "inbox").resolve()
    assert _open(client, "export", "prj_x").status_code == 204 and opener.opened[-1] == kit.resolve()
    assert _open(client, "tripo_pack", "DS-p-a-hair-0-abc123").status_code == 204 and opener.opened[-1] == pack.resolve()
    assert _open(client, "polish_pack", "DS-p-a-hair-0-polish").status_code == 204 and opener.opened[-1] == polish.resolve()
    assert len(opener.opened) == 5


@pytest.mark.parametrize("ident", ["../..", "..\\..", "C:\\Windows", "C:/Windows", "\\\\server\\share", "\\\\?\\C:\\", "a/b", "a\\b", ".hidden", "-x", " x", "x ",
                                   "x\x00y", "x:y", "x*", "x?", "%2e%2e", "~", "$HOME", "%USERPROFILE%", "a" * 121, "con", "x;calc", "x&calc", "x|calc", "$(calc)",
                                   "`calc`", "x\ny"])
def test_open_folder_refuses_ids_that_are_not_plain_tokens(client, opener, ident):
    for kind in ("export", "tripo_pack", "polish_pack"):
        r = _open(client, kind, ident)
        assert r.status_code in (404, 422), (kind, ident, r.status_code)
        assert opener.opened == []


def test_open_folder_refuses_a_free_path_unknown_kinds_and_extra_fields(client, opener):
    for body in ({"kind": "path", "id": "C:\\Windows"}, {"kind": "export", "id": "prj_x", "path": "C:\\Windows"}, {"kind": "export", "path": "/etc"},
                 {"path": "C:\\Windows"}, {"kind": "logs", "id": "", "command": "calc"}, {"kind": ["logs"]}, {}, {"kind": "tripo_inbox"}, {"kind": "LOGS"}):
        r = client.post("/api/os/open-folder", json=body)
        assert r.status_code == 422, body
    assert opener.opened == []


def test_open_folder_needs_the_token_and_the_origin(anon, client, opener):
    assert anon.post("/api/os/open-folder", json={"kind": "logs"}).status_code == 403
    assert client.post("/api/os/open-folder", json={"kind": "logs"}, headers={"Origin": "http://evil.example"}).status_code == 403
    assert client.get("/api/os/open-folder").status_code == 405
    assert opener.opened == []


def test_open_folder_refuses_a_folder_outside_data_and_exports(client, rt, opener, tmp_path):
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    exports = tmp_path / "exports"
    exports.mkdir()
    rt.update_settings({"paths": {"exports_root": str(exports), "tripo_inbox": str(outside)}})
    rt.repo.kv_set("export:prj_o", {"kit_dir": str(outside)})
    rt.repo.kv_set("pack:prj_o:a.hair", {"pack_id": "DS-o", "folder": "/"})
    rt.repo.kv_set("export:prj_r", {"kit_dir": str(tmp_path.parent)})
    for kind, ident in (("inbox", ""), ("export", "prj_o"), ("tripo_pack", "DS-o"), ("export", "prj_r")):
        r = _open(client, kind, ident)
        assert r.status_code == 403 and r.json()["error"] == "outside_app_folders", (kind, r.text)
    assert opener.opened == []


def test_open_folder_refuses_a_file_and_a_symlink_that_leads_out(client, rt, opener, tmp_path):
    exports = tmp_path / "exports"
    exports.mkdir()
    rt.update_settings({"paths": {"exports_root": str(exports), "tripo_inbox": str(exports / "inbox")}})
    exe = exports / "run.exe"
    exe.write_bytes(b"MZ")
    rt.repo.kv_set("export:prj_f", {"kit_dir": str(exe)})
    r = _open(client, "export", "prj_f")
    assert r.status_code == 422 and r.json()["error"] == "not_a_folder"
    if os.name != "nt":
        outside = tmp_path / "outside"
        outside.mkdir()
        (exports / "junction").symlink_to(outside, target_is_directory=True)
        rt.repo.kv_set("export:prj_l", {"kit_dir": str(exports / "junction")})
        assert _open(client, "export", "prj_l").status_code == 403
    assert _open(client, "export", "prj_missing").status_code == 404
    assert opener.opened == []


def test_open_folder_without_windows_says_unavailable_and_launches_nothing(client, rt, monkeypatch):
    from duoskin import winplat
    from duoskin.api import os_open

    launched: list = []
    monkeypatch.setattr(os, "startfile", lambda *a, **k: launched.append(a), raising=False)
    monkeypatch.setattr(winplat, "IS_WINDOWS", False)
    assert os_open.OPENER["fn"] is os_open._startfile
    r = _open(client, "logs")
    assert r.status_code == 501 and r.json()["error"] == "unavailable" and launched == []


def test_on_windows_only_a_resolved_directory_string_reaches_startfile(client, rt, monkeypatch):
    from duoskin import winplat
    from duoskin.api import os_open

    launched: list = []
    monkeypatch.setattr(os, "startfile", lambda *a, **k: launched.append((a, k)), raising=False)
    monkeypatch.setattr(winplat, "IS_WINDOWS", True)
    assert _open(client, "logs").status_code == 204
    assert launched == [((str(rt.paths.logs_dir.resolve()),), {})] and os_open.OPENER["fn"] is os_open._startfile


def test_nothing_in_the_package_can_run_a_user_controlled_command():
    """Static audit: no ``shell=True``, ``os.system``, ``os.popen``, ``eval``/``exec``, ``pickle``; ``startfile`` only in ``api/os_open.py``;
    every ``subprocess`` call passes a list (never a string)."""
    bad: list[str] = []
    startfile_sites: list[str] = []
    for path in sorted((APP_ROOT / "duoskin").rglob("*.py")):
        if "blender_scripts" in path.parts:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        rel = path.relative_to(APP_ROOT).as_posix()
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                name = ast.unparse(node.func)
                if name in ("os.system", "os.popen", "eval", "exec", "pickle.load", "pickle.loads", "marshal.loads", "yaml.load"):
                    bad.append(f"{rel}:{node.lineno} {name}")
                if name.endswith("startfile"):
                    startfile_sites.append(rel)
                if name.startswith("subprocess.") or name.endswith("Popen"):
                    if any(kw.arg == "shell" and not (isinstance(kw.value, ast.Constant) and kw.value.value is False) for kw in node.keywords):
                        bad.append(f"{rel}:{node.lineno} shell=")
                    if node.args and isinstance(node.args[0], (ast.Constant, ast.JoinedStr)):
                        bad.append(f"{rel}:{node.lineno} a command string")
    assert bad == []
    assert set(startfile_sites) <= {"duoskin/api/os_open.py"}, startfile_sites


def test_every_route_that_changes_something_is_guarded_by_the_token(app):
    """Walk the real route table (from the OpenAPI schema): every non-GET route must be refused without the token. The middleware guards by
    method, so a route added later cannot forget."""
    import re

    anon = make_client(app, authed=False)
    seen = 0
    for path, ops in app.openapi()["paths"].items():
        if not path.startswith("/api"):
            continue
        concrete = re.sub(r"\{[^}]+\}", "x", path)
        for method in ops:
            if method.upper() in ("GET", "HEAD", "OPTIONS"):
                continue
            r = anon.request(method.upper(), concrete)
            assert r.status_code == 403 and r.json() == {"error": "bad_token"}, (method, path, r.status_code)
            seen += 1
    assert seen >= 20


def test_the_server_address_file_holds_no_token(rt):
    from duoskin import config

    config.write_server_info(rt.paths, port=8765, instance_id=rt.instance_id)
    text = rt.paths.server_json.read_text(encoding="utf-8")
    assert rt.token not in text and "token" not in text.lower() and "127.0.0.1" in text


def test_uvicorn_is_started_without_proxy_headers_or_a_server_banner():
    src = (APP_ROOT / "duoskin" / "__main__.py").read_text(encoding="utf-8")
    assert "proxy_headers=False" in src and "server_header=False" in src


def test_the_listening_socket_is_loopback_only_and_refuses_other_hosts():
    from duoskin import winplat

    for host in ("0.0.0.0", "", "::", "192.168.1.5", "localhost.evil.com", "127.0.0.2.evil"):
        with pytest.raises(ValueError):
            winplat.bind_socket(0, host=host)


def test_the_settings_api_refuses_device_paths_dotdot_and_nul_but_allows_a_share_or_a_drive(client, rt):
    for path in ("\\\\?\\C:\\Windows", "\\\\.\\pipe\\x", "//?/C:/x", "C:\\Users\\..\\Windows", "../up", "x\x00y", ""):
        for key in ("exports_root", "tripo_inbox"):
            r = client.put("/api/settings", json={"paths": {key: path}})
            assert r.status_code == 422, (key, path, r.status_code)
    for ok in ("C:\\Users\\me\\DuoSkin Exports", "\\\\nas\\share\\DuoSkin", "%USERPROFILE%\\DuoSkin Exports", "D:\\Models"):
        r = client.put("/api/settings", json={"paths": {"exports_root": ok}})
        assert r.status_code == 200 and r.json()["paths"]["exports_root"] == ok, ok
