"""imaging/colournames.py: the colour dictionary (no banned term), nearest names by CIEDE2000, the 5 skin tones."""
from __future__ import annotations

import itertools
import json
import re
from pathlib import Path

import pytest

from duoskin.imaging import colournames as C
from duoskin.imaging import palette as P

DATA = Path(__file__).resolve().parents[2] / "duoskin" / "data"


def test_dictionary_is_well_formed_and_has_no_banned_or_age_coded_word():
    names = C.load_names()
    assert len(names) >= 100
    for name, hx in names.items():
        assert re.fullmatch(r"[a-z][a-z ]*", name), name           # human words only, lower-case, no digits or punctuation
        assert re.fullmatch(r"#[0-9a-f]{6}", hx)
        assert len(name.split()) <= 3
    assert C.find_banned_names() == []                            # the full data/banned_terms.json (all groups)
    assert C.find_banned_names(C.FALLBACK_BANNED) == []
    raw = json.loads((DATA / "colour_names.json").read_text(encoding="utf-8"))
    assert set(raw["colours"]) == set(names)


def test_banned_name_scan_is_whole_word_and_punctuation_insensitive(monkeypatch):
    monkeypatch.setattr(C, "load_names", lambda *a, **k: {"off white": "#efeae0", "baby blue": "#89cff0", "babylon": "#111111", "plain": "#222222"})
    assert C.find_banned_names(["Off-White", "baby"]) == ["off white", "baby blue"]       # babylon is not "baby"
    assert C.find_banned_names(["tiffany"]) == []
    assert C.find_banned_names([]) == []
    assert C._fold("  Off--White ") == "off white"
    assert "off-white" in C.banned_terms_from_data()                                       # the data file is the real source


def test_banned_terms_fall_back_when_the_data_file_is_missing(monkeypatch, tmp_path):
    monkeypatch.setattr(C, "_DATA", tmp_path)
    assert C.banned_terms_from_data() == list(C.FALLBACK_BANNED)
    (tmp_path / "banned_terms.json").write_text("{not json", encoding="utf-8")
    assert C.banned_terms_from_data() == list(C.FALLBACK_BANNED)
    (tmp_path / "banned_terms.json").write_text(json.dumps({"groups": {"a": ["x y"], "b": ["z"]}}), encoding="utf-8")
    assert C.banned_terms_from_data() == ["x y", "z"]


def test_nearest_names_are_closest_first_and_deterministic():
    near = C.nearest_names("#c82828", 3)
    assert len(near) == 3 and [n.de for n in near] == sorted(n.de for n in near)
    assert near[0].de < 12 and near[0].name in {"red", "crimson", "scarlet", "brick red", "fire engine red"} or "red" in near[0].name
    assert C.nearest_names("#c82828", 3) == near
    exact = C.nearest_names(C.load_names()["red"], 1)[0]
    assert exact.name == "red" and exact.de == pytest.approx(0.0, abs=1e-6) and exact.hex == C.load_names()["red"]
    assert next(n.name for n in C.nearest_names("#c82828", 2, exclude=[near[0].name])) == near[1].name
    assert C.colour_name("#c82828") == near[0].name


def test_name_palette_gives_distinct_names_and_at_most_three():
    out = C.name_palette(["#c82828", "#c92929", "#141414", "#2f4fb4", "#f2f2f2"])
    assert len(out) == len(set(out)) == C.MAX_NAMES_PER_PROMPT
    assert C.name_palette(["#c82828", "#c82828"]) == [C.nearest_names("#c82828", 1)[0].name, C.nearest_names("#c82828", 2)[1].name]
    assert C.name_palette([]) == []
    assert len(C.name_palette(["#c82828", "#141414"], max_names=1)) == 1


def test_a_prompt_for_a_palette_never_contains_a_hex_code():
    text = " ".join(C.name_palette(["#c82828", "#141414", "#2f4fb4"]))
    assert "#" not in text and not re.search(r"[0-9a-f]{6}", text)


def test_five_skin_tones_from_data_in_order_of_darkness_and_distinct():
    tones = C.load_skin_tones()
    assert [t.id for t in tones] == [f"tone_{i}" for i in range(1, 6)]
    lightness = [P.hex_to_lab(t.hex)[0] for t in tones]
    assert lightness == sorted(lightness, reverse=True)
    assert all(a != b for a, b in itertools.pairwise(lightness))
    assert C.skin_tone_hex("tone_1") == "#f6dcc8" and C.skin_tone_hex("tone_5") == "#3a2218"
    with pytest.raises(KeyError):
        C.skin_tone_hex("tone_6")
    raw = json.loads((DATA / "skin_tones.json").read_text(encoding="utf-8"))
    assert len(raw["tones"]) == 5
    assert all(float(P.deltaE2000(P.hex_to_lab(a.hex)[None, :], P.hex_to_lab(b.hex)[None, :])[0]) > 10
               for i, a in enumerate(tones) for b in tones[i + 1:])
