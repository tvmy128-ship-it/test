"""SYS-02, SYS-04, SYS-13, ENG-11 against the real app: Host, Origin, token, headers, CAS sandbox, MIME, path safety."""
from __future__ import annotations

import pytest

from duoskin.engine.cas import make_prov
from duoskin.engine.testkit import png_bytes

UNSAFE = [("POST", "/api/projects"), ("PUT", "/api/settings"), ("PUT", "/api/keys/openai"), ("DELETE", "/api/keys/openai"),
          ("POST", "/api/keys/openai/test"), ("POST", "/api/shutdown"), ("POST", "/api/doctor/run"), ("POST", "/api/diagnostics"),
          ("POST", "/api/focus"), ("PATCH", "/api/projects/prj_x"), ("POST", "/api/projects/prj_x/plan"),
          ("POST", "/api/jobs/job_x/cancel"), ("POST", "/api/steps/stp_x/retry"), ("POST", "/api/gates/gat_x/decisions"),
          ("DELETE", "/api/gates/gat_x/decisions/dec_x"), ("POST", "/api/projects/prj_x/export")]


@pytest.mark.parametrize("method,path", UNSAFE)
def test_every_mutating_route_refuses_a_missing_token(anon, method, path):
    r = anon.request(method, path)
    assert r.status_code == 403 and r.json() == {"error": "bad_token"}


@pytest.mark.parametrize("method,path", UNSAFE)
def test_every_mutating_route_refuses_a_foreign_origin(client, method, path):
    r = client.request(method, path, headers={"Origin": "http://evil.example"})
    assert r.status_code == 403 and r.json() == {"error": "bad_origin"}


def test_a_wrong_token_is_refused(app):
    from duoskin.engine.testkit import make_client

    c = make_client(app, authed=False)
    c.headers["X-DuoSkin-Token"] = "not-the-token"
    assert c.post("/api/doctor/run").status_code == 403


def test_get_routes_need_no_token_but_still_check_the_host(anon):
    assert anon.get("/api/health").status_code == 200
    assert anon.get("/api/health", headers={"Host": "evil.example"}).status_code == 400
    assert anon.get("/api/state", headers={"Host": "attacker.com:8765"}).status_code == 400          # DNS rebinding
    assert anon.get("/", headers={"Host": "evil.example"}).status_code == 400
    assert anon.get("/api/health", headers={"Host": "localhost:8765"}).status_code == 200


def test_the_default_testserver_host_is_rejected_per_sys22(app):
    from starlette.testclient import TestClient

    assert TestClient(app).get("/api/health").status_code == 400                                      # why make_client exists


def test_no_cors_headers_anywhere(client):
    for path in ("/api/health", "/api/state", "/", "/web/app.js"):
        r = client.get(path, headers={"Origin": "http://evil.example"})
        assert not any(k.lower().startswith("access-control-") for k in r.headers), path
    r = client.options("/api/health", headers={"Origin": "http://evil.example", "Access-Control-Request-Method": "GET"})
    assert "access-control-allow-origin" not in {k.lower() for k in r.headers}


def test_html_security_headers(client):
    r = client.get("/")
    csp = r.headers["content-security-policy"]
    assert csp.startswith("default-src 'self'") and "frame-ancestors 'none'" in csp and "'unsafe-inline'" not in csp and "'unsafe-eval'" not in csp
    assert r.headers["x-content-type-options"] == "nosniff" and r.headers["referrer-policy"] == "no-referrer"
    assert r.headers["cache-control"] == "no-store" and "set-cookie" not in r.headers


def test_api_json_is_no_store_and_nosniff(client):
    r = client.get("/api/state")
    assert r.headers["cache-control"] == "no-store" and r.headers["x-content-type-options"] == "nosniff"


def test_the_index_never_serves_a_stale_token(app, client):
    first = client.get("/").text
    token = app.state.rt.token
    assert token in first and client.get("/").headers["cache-control"] == "no-store"
    assert client.get("/index.html").text == first


def test_the_shell_page_has_no_inline_script_or_style(client):
    html = client.get("/").text
    import re

    assert not re.search(r"<script(?![^>]*\bsrc=)[^>]*>\s*\S", html) and " style=" not in html and "<style" not in html
    assert not re.search(r"\son[a-z]+=", html)


# ------------------------------------------------------------------------------------------------- CAS
@pytest.fixture
def asset(rt):
    return rt.cas.put(png_bytes(), "png", prov=make_prov("code"))


def test_cas_files_are_sandboxed_and_immutable(client, asset):
    r = client.get(f"/cas/{asset.sha256}.png")
    assert r.status_code == 200 and r.content == png_bytes() and r.headers["content-type"] == "image/png"
    assert r.headers["x-content-type-options"] == "nosniff" and r.headers["content-security-policy"] == "sandbox"
    assert r.headers["cache-control"] == "public, max-age=31536000, immutable"


def test_an_svg_or_html_asset_cannot_run_script_in_our_origin(client, rt):
    svg = rt.cas.put(b"<svg xmlns='http://www.w3.org/2000/svg'><script>alert(1)</script></svg>", "svg", prov=make_prov("recraft"))
    html = rt.cas.put(b"<script>alert(1)</script>", "html", prov=make_prov("code"))
    for a, ext in ((svg, "svg"), (html, "html")):
        r = client.get(f"/cas/{a.sha256}.{ext}")
        assert r.status_code == 200 and r.headers["content-security-policy"] == "sandbox" and r.headers["x-content-type-options"] == "nosniff"
    assert client.get(f"/cas/{svg.sha256}.svg").headers["content-type"] == "image/svg+xml"


@pytest.mark.parametrize("name", ["../../etc/passwd", "..%2f..%2fetc%2fpasswd", "x.png", "a" * 63 + ".png", "A" * 64 + ".png", "g" * 64 + ".png",
                                  "a" * 64, "a" * 64 + ".", "a" * 64 + ".png/../x", "a" * 64 + ".toolongextension", "%00", "a" * 64 + ".p%00ng"])
def test_cas_names_are_regex_checked_and_never_touch_the_filesystem(client, name):
    assert client.get(f"/cas/{name}").status_code in (404, 400)


def test_cas_requires_the_right_extension_and_a_known_hash(client, asset):
    assert client.get(f"/cas/{asset.sha256}.jpg").status_code == 404
    assert client.get("/cas/" + "0" * 64 + ".png").status_code == 404


def test_cas_does_not_serve_a_file_whose_row_is_missing(client, rt):
    stray = rt.cas.root / "ab" / ("e" * 64 + ".png")
    stray.parent.mkdir(parents=True, exist_ok=True)
    stray.write_bytes(png_bytes())
    assert client.get(f"/cas/{'e' * 64}.png").status_code == 404                                       # only files the DB knows


# ------------------------------------------------------------------------------------------------- MIME and static safety
def test_js_is_javascript_even_if_the_registry_says_otherwise(tmp_path):
    import mimetypes

    mimetypes.add_type("text/plain", ".js")
    from duoskin.engine.testkit import make_app, make_client

    c = make_client(make_app(tmp_path / "m"))
    assert c.get("/web/app.js").headers["content-type"].startswith("text/javascript")


def test_static_directory_traversal_is_blocked(client):
    for p in ("/web/../../pyproject.toml", "/web/%2e%2e/app.py", "/web/..%2fapp.py", "/web//etc/passwd"):
        assert client.get(p).status_code in (400, 404)
    assert "app.py" not in client.get("/web/").text


def test_the_server_never_binds_beyond_loopback_by_construction():
    from duoskin import winplat

    s = winplat.bind_socket(0)
    try:
        assert s.getsockname()[0] == "127.0.0.1"
    finally:
        s.close()
    with pytest.raises(ValueError):
        winplat.bind_socket(0, host="0.0.0.0")
