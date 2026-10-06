"""``tools/export_dreamsim_onnx.py``: the argument parsing and the manifest writer only (the torch half cannot run here)."""
from __future__ import annotations

import hashlib
import importlib.util
import json

import pytest

from duoskin import config

TOOL = config.APP_ROOT / "tools" / "export_dreamsim_onnx.py"
SHA = hashlib.sha256(b"model").hexdigest()


@pytest.fixture(scope="module")
def tool():
    spec = importlib.util.spec_from_file_location("tool_export_dreamsim_onnx", TOOL)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)          # importing must not need torch
    return mod


def test_the_tool_pins_what_the_spec_says_and_imports_without_torch(tool):
    assert tool.DREAMSIM_VERSION and tool.OPSET >= 11 and tool.INPUT_SIZE == 224
    assert tool.FIXTURE_TOL == 0.01
    assert "torch" not in vars(tool)               # torch is only imported inside export()


def test_arguments_parse_with_defaults(tool, tmp_path):
    a = tool.parse_args(["--out", str(tmp_path / "o"), "--fixtures", str(tmp_path / "f")])
    assert a.opset == tool.OPSET and a.tol == tool.FIXTURE_TOL and a.expect_sha256 == "" and not a.allow_version_mismatch
    b = tool.parse_args(["--out", "o", "--fixtures", "f", "--opset", "18", "--tol", "0.02", "--expect-sha256", SHA.upper(), "--allow-version-mismatch"])
    assert (b.opset, b.tol, b.expect_sha256, b.allow_version_mismatch) == (18, 0.02, SHA, True)


@pytest.mark.parametrize("bad", [["--out", "o"], ["--fixtures", "f"],
                                 ["--out", "o", "--fixtures", "f", "--opset", "9"],
                                 ["--out", "o", "--fixtures", "f", "--tol", "0"],
                                 ["--out", "o", "--fixtures", "f", "--tol", "0.5"],
                                 ["--out", "o", "--fixtures", "f", "--expect-sha256", "abc"]])
def test_bad_arguments_exit_2(tool, bad):
    with pytest.raises(SystemExit) as e:
        tool.parse_args(bad)
    assert e.value.code == 2


def test_manifest_carries_everything_doctor_reads(tool):
    m = tool.build_manifest(sha256=SHA, expectations=[{"pair": ["a.png", "b.png"], "distance": 0.123456789}])
    assert m["sha256"] == SHA and m["opset"] == tool.OPSET and m["input_size"] == 224
    assert m["dreamsim_version"] == tool.DREAMSIM_VERSION and m["preprocessing"]
    assert m["fixture_expectations"] == [{"pair": ["a.png", "b.png"], "distance": 0.123457}]


@pytest.mark.parametrize("sha", ["", "xyz", SHA.upper(), SHA[:-1]])
def test_manifest_refuses_a_bad_hash(tool, sha):
    with pytest.raises(tool.ExportError):
        tool.build_manifest(sha256=sha, expectations=[{"pair": ["a", "b"], "distance": 0.1}])


def test_manifest_refuses_no_fixture_expectations(tool):
    with pytest.raises(tool.ExportError):
        tool.build_manifest(sha256=SHA, expectations=[])


def test_the_manifest_writer_is_utf8_atomic_and_repeatable(tool, tmp_path):
    m = tool.build_manifest(sha256=SHA, expectations=[{"pair": ["a.png", "b.png"], "distance": 0.5}])
    path = tool.write_manifest(tmp_path / "deep" / tool.MANIFEST_NAME, m)
    first = path.read_bytes()
    assert json.loads(first.decode("utf-8")) == m and b"\r\n" not in first
    assert not list(path.parent.glob("*.tmp"))
    tool.write_manifest(path, m)
    assert path.read_bytes() == first            # a re-export of the same model is byte-identical


def test_the_manifest_is_what_the_doctor_check_reads(tool, tmp_path):
    from duoskin.api import doctor_checks as dc

    assert dc.DREAMSIM_FILE == tool.MODEL_NAME
    side = tool.write_manifest(tmp_path / tool.MANIFEST_NAME,
                               tool.build_manifest(sha256=SHA, expectations=[{"pair": ["a.png", "b.png"], "distance": 0.25}]))
    meta = json.loads(side.read_text(encoding="utf-8"))
    assert meta["fixture_expectations"][0]["pair"] == ["a.png", "b.png"] and float(meta["fixture_expectations"][0]["distance"]) == 0.25
    assert dc.SHA_HEX.match(meta["sha256"])


def test_sha256_file_streams_the_file(tool, tmp_path):
    p = tmp_path / "m.onnx"
    p.write_bytes(b"x" * (3 << 20) + b"tail")
    assert tool.sha256_file(p) == hashlib.sha256(b"x" * (3 << 20) + b"tail").hexdigest()


def test_agreement_check_passes_inside_the_tolerance_and_refuses_outside(tool):
    assert tool.check_agreement([0.30, 0.55], [0.305, 0.549], 0.01) == pytest.approx(0.005)
    with pytest.raises(tool.ExportError, match="differ"):
        tool.check_agreement([0.30], [0.35], 0.01)
    with pytest.raises(tool.ExportError):
        tool.check_agreement([0.3], [0.3, 0.4], 0.01)
    with pytest.raises(tool.ExportError):
        tool.check_agreement([], [], 0.01)


def test_pairs_file_is_validated(tool, tmp_path):
    (tmp_path / "a.png").write_bytes(b"x")
    (tmp_path / "b.png").write_bytes(b"x")
    (tmp_path / "pairs.json").write_text(json.dumps([["a.png", "b.png"], {"pair": ["b.png", "a.png"]}]), encoding="utf-8")
    assert tool.read_pairs(tmp_path) == [("a.png", "b.png"), ("b.png", "a.png")]
    (tmp_path / "pairs.json").write_text(json.dumps([["a.png", "gone.png"]]), encoding="utf-8")
    with pytest.raises(tool.ExportError, match="missing"):
        tool.read_pairs(tmp_path)
    (tmp_path / "pairs.json").write_text("[]", encoding="utf-8")
    with pytest.raises(tool.ExportError):
        tool.read_pairs(tmp_path)
    (tmp_path / "pairs.json").write_text("{not json", encoding="utf-8")
    with pytest.raises(tool.ExportError):
        tool.read_pairs(tmp_path)


def test_main_reports_a_refusal_as_exit_1_without_torch(tool, tmp_path, capsys):
    rc = tool.main(["--out", str(tmp_path / "o"), "--fixtures", str(tmp_path / "f")])
    assert rc == 1 and "export refused" in capsys.readouterr().err
