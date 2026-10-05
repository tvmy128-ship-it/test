"""The single, versioned threshold registry (FAILURE_MODES §4.1, plus the keys added for issues #15/#78).

Checks read every number from here and never hard-code one. Each entry is ``name -> (value, status, failure-mode ids)``.

Status markers (FAILURE_MODES §0.2): ``DOC`` official doc or validator default, ``SPEC`` decided workflow or user
requirement, ``DER`` derived from local analysis, ``DES`` design choice (calibrate on labels at about the 5th percentile
of approved duos), ``UNV`` unverified claim.

Thresholds only cut the bad tail. No entry describes a target look (APP_SPEC §3.1): there are no ratios or counts that
say what a "good" duo should be.
"""
from __future__ import annotations

import contextlib
import contextvars
from collections.abc import Iterator
from typing import Any

THRESHOLDS_VERSION = "2026-10-05.1"

Status = str  # DOC | SPEC | DER | DES | UNV
STATUSES = ("DOC", "SPEC", "DER", "DES", "UNV")
TUNABLE_STATUSES = ("DES", "UNV")  # the only ones that calibration or demotion may touch

# fmt: off
T: dict[str, tuple[Any, Status, list[str]]] = {
    # ---- classic template (CLO) ----
    "tpl.size_wh":                ((585, 559), "DOC", ["CLO-02"]),
    "tpl.gap_px":                 (2, "DER", ["CLO-03"]),
    "tpl.open_side_bleed_px":     ((2, 4), "DES", ["CLO-03"]),
    "tpl.seam_de_mean_max":       (6.0, "DES", ["CLO-04"]),
    "tpl.seam_de_max":            (15.0, "DES", ["CLO-04"]),
    "tpl.split_rows_torso":       ((170,), "DER", ["CLO-07"]),
    "tpl.split_rows_limb":        ((418.5, 467), "DER", ["CLO-07"]),
    "tpl.split_margin_px":        (2, "DES", ["CLO-07"]),
    "tpl.shoe_top_row_range":     ((446, 465), "DER", ["CLO-07"]),
    "tpl.limb_bands":             (((355, 416), (421, 465), (469, 482)), "DER", ["CLO-07"]),
    "tpl.bevel_inset_px":         (5, "DER", ["CLO-08"]),
    "tpl.hidden_leg_rows":        ((355, 377), "UNV", ["CLO-08"]),   # T1 measures it (X24)
    "tpl.semi_alpha_share_max":   (0.005, "DES", ["CLO-10"]),
    "tpl.skin_in_clothing_de":    (6.0, "DES", ["CLO-11"]),
    "fabric.seam_energy_ratio":   (1.5, "DES", ["CLO-13"]),
    "fabric.alias_energy_max":    (0.10, "DES", ["CLO-13"]),
    "fabric.flat_lum_std_max":    (0.03, "DES", ["CLO-13"]),
    "fabric.chroma_max":          (3.0, "DES", ["CLO-13"]),         # bible I7 says 3, CLO-13 said 2: the looser value wins
    "fold.mean_grey":             ((120, 136), "DES", ["CLO-14"]),
    "fold.edge_band_std_max":     (3.0, "DES", ["CLO-14"]),
    "fold.edge_band_l_max":       (2.0, "DES", ["CLO-14"]),         # bible I8 edge band (L*), kept next to the std rule
    # ---- generic 2D assets (IMG) ----
    "img.clear_share_min":        (0.10, "DES", ["IMG-01"]),
    "img.border_frame":           (0.02, "DES", ["IMG-01"]),
    "img.haze_share_max":         (0.03, "DES", ["IMG-02"]),
    "img.halo_de_max":            (8.0, "DES", ["IMG-02"]),
    "img.margin_min":             (0.06, "DES", ["IMG-03", "ACC-03"]),
    "img.component_min_area":     (0.002, "DES", ["IMG-04"]),
    "img.palette_de_max":         (12.0, "DES", ["IMG-05", "CON-01", "DUO-07"]),
    "img.palette_large_reject_de":(15.0, "DES", ["IMG-05"]),
    "img.palette_min_cluster":    (0.02, "DES", ["IMG-05"]),        # bible A_PALETTE: clusters >= 2% of the area count
    "img.palette_large_share":    (0.05, "DES", ["IMG-05"]),        # a cluster >= 5% of the foreground is "large"
    "img.final_iou_min":          (0.92, "DES", ["IMG-06"]),
    "img.final_de_max":           (5.0, "DES", ["IMG-06"]),
    "img.reimagine_phash_max":    (6, "DES", ["IMG-08"]),
    "img.gradient_share_max":     (0.05, "DES", ["IMG-09", "IMG-16"]),
    "img.stroke_px_min_placed":   (2.0, "DES", ["IMG-16"]),         # thinnest stroke at the placed (final texel) size
    "img.shading_steps_max":      (2, "DES", ["IMG-09"]),           # SOFT ranking inputs
    "img.specular_share_max":     (0.01, "DES", ["IMG-09"]),
    "img.hf_energy_ratio_max":    (1.5, "DES", ["IMG-09"]),         # high-frequency energy <= profile x 1.5
    "img.stroke_ratio_band":      ((0.7, 1.3), "DES", ["IMG-09"]),
    "img.symmetry_iou_min":       (0.90, "DES", ["IMG-10"]),
    "img.sentinel_de_min":        (40.0, "DES", ["IMG-12"]),
    "img.pasteback_ring_px":      ((4, 8), "DES", ["GEN-03"]),
    "img.pasteback_ring_de_max":  (3.0, "DES", ["GEN-03"]),
    "img.styleref_phash_max":     (10, "DES", ["IMG-15"]),
    "img.styleref_dreamsim_min":  (0.25, "DES", ["IMG-15"]),
    "img.checker_peak_ratio":     (0.12, "DES", ["IMG-01"]),        # FFT periodic-peak share of the non-DC spectrum energy
    "img.edge_lum_ratio_min":     (0.90, "DES", ["IMG-13"]),        # edge luminance >= 90% of interior neighbours
    "img.alpha_binarize":         (128, "DES", ["IMG-02"]),         # cel-art alpha threshold (bible 2.5)
    "img.glyph_score_max":        (0.30, "DES", ["POL-04"]),        # A_GLYPH: score < 0.3
    "img.slab_solidity_min":      (0.80, "DES", ["ACC-18"]),        # I6 badge: solidity (area / convex hull) >= 0.8
    "img.badge_outline_min":      (0.03, "DES", ["ACC-18"]),        # I6 A_STROKE: outline >= 3% of the width
    "img.guide_left_max":         (0.005, "DES", ["HAIR-04"]),      # A_GUIDE_LEFT: guide-grey pixels < 0.5%
    "img.highlight_max_blobs":    (0, "SPEC", ["FACE-04"]),         # A_HIGHLIGHT: no white blobs inside the iris
    "img.views_height_tol":       (0.03, "DES", ["ACC-03"]),        # A_VIEWS heights
    "img.views_ground_tol":       (0.01, "DES", ["ACC-03"]),        # A_VIEWS ground line
    "img.views_centre_tol":       (0.02, "DES", ["ACC-03"]),        # A_VIEWS centring
    "svg.max_paths":              (300, "DES", ["IMG-14"]),
    "svg.border_sentinel_min":    (0.95, "DES", ["IMG-14"]),
    "svg.matte_edge_err_max":     (6, "DES", ["IMG-14"]),           # two-pass matte edge error, /255
    "svg.snap_de_max":            (15.0, "DES", ["IMG-14"]),        # a large fill farther than this from the palette is rejected
    "ocr.rec_score_min":          (0.5, "DES", ["POL-04"]),
    "ocr.min_box_px":             (8, "DES", ["POL-04"]),
    # ---- concept (CON) ----
    "con.body_iou_min":           (0.85, "DES", ["CON-02"]),
    "con.volume_outside_max":     (0.03, "DES", ["CON-02"]),
    "con.leak_area_max":          (0.03, "DES", ["CON-01"]),
    "con.partner_only_de":        (12.0, "DES", ["CON-01"]),        # partner-only colour = farther than this from every own colour
    "con.clone_proxy_dreamsim_min":(0.20, "UNV", ["CON-09"]),       # concept-level clone proxy warning (cold-start value)
    "con.palette_snap_de":        (10.0, "DES", ["CON-05"]),
    "con.swatch_de_warn":         (15.0, "DES", ["CON-05"]),        # A_SWATCH (warning only)
    # ---- plan lint (PLN) ----
    "pln.anchors":                ((2, 3), "SPEC", ["PLN-03"]),   # decided workflow contract
    "pln.contrasts_min":          (5, "SPEC", ["PLN-03"]),
    "pln.face_features_diff_min": (3, "SPEC", ["PLN-03", "FACE-11"]),
    "pln.contrast_colour_de":     (15.0, "DES", ["PLN-04"]),
    "pln.contrast_value_dl":      (15.0, "DES", ["PLN-04"]),
    "pln.anchor_colour_de_max":   (6.0, "DES", ["PLN-06", "DUO-02"]),
    "pln.lash_iris_de_min":       (10.0, "DES", ["PLN-13", "FACE-10"]),
    "pln.adjacent_de_min":        (10.0, "DES", ["PLN-13"]),
    "pln.acc_per_char_warn_hard": ((3, 4), "DES", ["PLN-14"]),
    "pln.kit_hair_iou_warn":      (0.85, "DES", ["PLN-15"]),
    "pln.mirror_role_de_max":     (10.0, "DES", ["PLN-06"]),        # mirror profile: a_main ~ b_second within this
    "pln.dna_char_diff_min":      (2, "SPEC", ["PLN-DNA-01"]),      # PLN-DNA-01: >= 2 differing CHARACTER DNA fields
    "pln.identical_fields_same_club_extra": (2, "UNV", ["PLN-03"]),  # same_club may share this many more fields
    "pln.identical_fields_max":   (6, "UNV", ["PLN-03"]),           # bible C1 #13 clone proxy N [CALIBRATE]
    # pln.accessory_cat_jaccard_max was removed: the (kind, category, motif) rule replaced it (issues #41, #58).
    # ---- face (FACE) ----
    "face.line_colours":          (1, "DOC", ["FACE-01"]),
    "face.lid_shade_alpha_max":   (0.35, "DES", ["FACE-01"]),
    "face.line_skin_de_min":      (20.0, "DES", ["FACE-06"]),
    "face.line_edge_ring_de_max": (8.0, "DES", ["FACE-06"]),
    "face.stroke_px_min":         (2.0, "DES", ["FACE-06"]),
    "face.stroke_px_min_2x_down": (1.0, "DES", ["FACE-06"]),
    "face.warp_iou_min":          (0.95, "DES", ["FACE-05"]),
    "face.facs_stretch_max":      (1.5, "DES", ["FACE-08"]),
    "face.registry_phash_max":    (8, "DES", ["FACE-11"]),
    "face.registry_dreamsim_min": (0.15, "DES", ["FACE-11"]),
    "face.registry_window_duos":  (30, "DES", ["FACE-11", "PLN-16"]),
    "face.skin_tones":            (5, "SPEC", ["FACE-06", "FACE-07"]),   # workflow requirement
    "face.hair_on_head_area_max": (0.005, "DES", ["FACE-16"]),
    "face.nonfeature_alpha_max":  (0.35, "DES", ["FACE-16"]),
    "face.blush_l_max":           (45.0, "DES", ["FACE-07"]),       # blush L* <= 45 so it darkens on every tone
    "face.blush_alpha_max":       (0.35, "DES", ["FACE-07"]),
    "face.lid_cover_min":         (0.99, "DES", ["FACE-03"]),       # 2D: closed-lid layer covers the sclera polygon
    "face.zone_inside_min":       (1.0, "DES", ["FACE-02"]),        # 2D: share of feature pixels inside their canvas zones
    "face.catchlight_tol_px":     (1.0, "DES", ["FACE-04"]),        # highlight offsets of both eyes agree within this
    "face.neck_seam_de_max":      (2.0, "DES", ["FACE-12"]),        # CHK-B09
    "face.skin_alpha_zero_min":   (0.95, "DES", ["FACE-12"]),       # >= 95% of the skin zone of the head texture is alpha 0
    "face.mouth_inner_min_alpha": (0.99, "DES", ["FACE-14"]),       # open-mouth interior painted
    "face.blink_iris_px_max":     (0, "SPEC", ["FACE-03"]),         # iris pixels in the blink render
    "uv.warp_density":            ((2, 4), "DES", ["FACE-05"]),     # LUT oversample before the downsample
    # ---- hair / accessories / mesh ----
    "hair.guide_iou_min":         (0.98, "DES", ["HAIR-03"]),
    "hair.face_protect_frac":     (0.55, "DES", ["HAIR-03"]),
    "hair.guide_de_min":          (30.0, "DES", ["HAIR-04"]),
    "hair.tris_target":           (3600, "DES", ["HAIR-07"]),
    "hair.kit_match_min":         (0.85, "DES", ["HAIR-08"]),
    "hair.concept_iou_min":       (0.75, "DES", ["HAIR-09"]),       # I4 hair front vs the concept crop (issue #15)
    "hair.front_face_visible_min":(0.80, "DES", ["HAIR-12"]),       # CHK-M21: head front face visible in the front render
    "hair.guide_texel_share_max": (0.005, "DES", ["HAIR-12"]),      # CHK-M21: guide-colour texels left in the hair texture
    "hair.head_cut_inflate_stud": (0.02, "DES", ["HAIR-12"]),       # head box inflation for the boolean cut
    "acc.view_height_tol":        (0.03, "DES", ["ACC-03"]),
    "acc.fill_long_side":         ((0.80, 0.85), "DES", ["ACC-03"]),
    "acc.thin_part_min_frac":     (0.02, "DES", ["ACC-03"]),
    "acc.view_iou_min":           (0.80, "DES", ["ACC-08"]),
    "acc.front_iou_min":          (0.85, "DES", ["ACC-08"]),
    "acc.mirror_margin":          (0.02, "DES", ["ACC-01"]),
    "acc.seeds":                  ((11, 29, 47), "DES", ["ACC-08"]),
    "acc.white_edge_de":          (10.0, "DES", ["ACC-03"]),       # flatten on #D9D9D9 instead of white
    "acc.view_palette_de_max":    (12.0, "DES", ["ACC-03", "ACC-15"]),  # per-view palette dE (bible said 10, ACC-15 says 12)
    "acc.lr_brightness_l_max":    (12.0, "DES", ["ACC-14"]),       # I5 left/right brightness difference (L*)
    "slab.thickness_min":         (0.08, "DES", ["ACC-18"]),       # RBX suggestion; Roblox publishes no number
    "slab.extent_ratio_min":      (0.03, "DES", ["ACC-18"]),
    "slab.back_mirror_phash_min": (10, "DES", ["ACC-17"]),
    "mesh.tris_max":              (3800, "DES", ["MESH-01"]),   # Roblox limit 4000 [DOC]
    "mesh.tex_warn_hard":         ((1024, 2048), "DOC", ["MESH-04"]),
    "mesh.surface_area_max":      (70.0, "DOC", ["MESH-07"]),   # UGCValidateMaxTotalSurfaceArea
    "mesh.coplanar_max_frac":     (0.15, "DOC", ["MESH-11"]),
    "mesh.center_offset_max":     (1.0, "DOC", ["MESH-11"]),
    "mesh.scale_min":             (0.01, "DOC", ["MESH-11"]),
    "mesh.components_max":        (10, "DOC", ["MESH-09"]),
    "mesh.shells_warn":           (8, "DES", ["MESH-09"]),
    "mesh.normals_out_min":       (0.99, "DES", ["MESH-08"]),
    "mesh.thickness_min":         (0.05, "DES", ["MESH-08"]),
    "mesh.sparse_cover_warn_fail":((0.50, 0.30), "UNV", ["MESH-10"]),
    "mesh.orient_iou_min":        (0.80, "DES", ["MESH-12"]),
    "mesh.clip_depth_max":        (0.02, "DES", ["MESH-16", "HAIR-06"]),
    "mesh.gap_max":               (0.10, "DES", ["MESH-16"]),
    # ---- duo ----
    "duo.dreamsim_clone_min":     (0.30, "DES", ["DUO-01"]),   # SUM value, tune on labels
    # degraded clone band (no dreamsim.onnx): a degraded clone only when ALL THREE trip (DUO-01)
    "duo.degraded_phash_clone_max":(10, "UNV", ["DUO-01"]),    # (a) mean A-vs-B pHash Hamming distance per side <= this trips
    "duo.degraded_palette_overlap_max":(0.50, "UNV", ["DUO-01"]),  # (b) palette overlap > this trips
    "duo.degraded_spec_dist_min": (0.35, "UNV", ["DUO-01"]),   # (c) share of differing PLN-03 fields < this trips
    "clone.lower_edge":           (0.30, "DES", ["DUO-01"]),   # A_CLONE duo stage (same value as duo.dreamsim_clone_min)
    "clone.concept_lower_edge":   (0.20, "UNV", ["DUO-01", "CON-09"]),  # cold-start; then ~5th percentile of approved pairs
    "clone.gate2_lower_edge":     (0.25, "UNV", ["DUO-01"]),   # cold-start; Gate 2 pre-build composites
    "dreamsim.fixture_tol":       (0.01, "DES", ["SYS-08"]),   # CHK-S14: fixture pair distance tolerance
    "duo.anchor_area_min":        (0.01, "DES", ["DUO-02"]),
    "duo.thumb_height_px":        ((120, 150), "DES", ["DUO-03"]),
    "duo.interpenetration_max":   (0.005, "DES", ["DUO-06"]),
    "duo.nearest_very_close":     (0.10, "UNV", ["DUO-10"]),   # DreamSim distance to a past duo that counts as "very close"
    "duo.strangers_dreamsim_max": (0.80, "UNV", ["DUO-01"]),   # strangers upper edge (soft)
    "duo.same_world_baseline":    (0.55, "UNV", ["DUO-08"]),   # same-world style baseline (soft)
    "pol.ref_dreamsim_min":       (0.25, "DES", ["POL-02"]),
    "pol.ref_phash_max":          (10, "DES", ["POL-02"]),
    # ---- taste warnings (SOFT forever; cold-start values until taste.cold_start_duos approved duos exist) ----
    "taste.cold_start_duos":      (5, "SPEC", ["DUO-04"]),
    "taste.acc_min_px":           (8, "UNV", ["DUO-04"]),
    "taste.silhouette_iou_warn":  (0.60, "UNV", ["PLN-15", "DUO-04"]),   # cold-start for the 95th percentile of approved duos
    "taste.layout_ari_warn":      (0.85, "UNV", ["DUO-04"]),             # garment layout adjusted-Rand band (cold start)
    "taste.plan_ratio_share_off": (15.0, "DES", ["DUO-03"]),             # declared-vs-built share off by more than 15 points
    # ---- calibration and learning loop ----
    "calib.demote_fire_rate":     (0.30, "SPEC", ["ENG-10"]),
    "calib.hard_reject_budget":   (0.10, "SPEC", ["ENG-10"]),
    "calib.registry_reject_alert":(0.15, "UNV", ["ENG-10"]),
    "calib.structure_collapse_share": (0.35, "SPEC", ["PLN-08"]),
    "calib.wildcard_pick_rate":   (0.30, "SPEC", ["PLN-08"]),
    "calib.first_try_approval":   (0.50, "UNV", ["ENG-10"]),
    "calib.variety_guard":        (0.05, "SPEC", ["ENG-10"]),
    # ---- process ----
    "budget.per_duo_usd":         (15.0, "DES", ["ENG-05"]),
    "ladder.max_fixes_per_part":  (3, "SPEC", ["ENG-05"]),       # workflow stop rule
    "revise.max_rounds":          (2, "SPEC", ["LLM-02"]),
    "gate.max_soft_warnings":     (2, "SPEC", ["ENG-10"]),       # DEC
    "gate.warning_override_hide": (0.25, "SPEC", ["ENG-10"]),    # DEC
    "vlm.min_side_px":            (256, "DES", ["VLM-01"]),
    "vlm.max_long_edge":          (2576, "DOC", ["VLM-01"]),
    "vlm.flip_rate_max":          (0.10, "DES", ["VLM-02"]),
    "vlm.agreement_min":          (0.90, "DOC", ["VLM-02"]),    # docs' eval suggestion
    "vlm.costly_votes":           ((2, 3), "DES", ["VLM-07"]),   # majority of 3; auto-approve needs 3 of 3
    "db.busy_timeout_ms":         (5000, "DES", ["ENG-13"]),
    "upload.max_mb":              (50, "DES", ["SYS-04"]),
    "upload.zip_max_total_mb":    (500, "DES", ["SYS-04"]),
    "upload.zip_max_files":       (200, "DES", ["SYS-04"]),
}
# fmt: on

_OVERRIDES: contextvars.ContextVar[dict[str, Any] | None] = contextvars.ContextVar("duoskin_threshold_overrides", default=None)


class UnknownThreshold(KeyError):
    """Raised for a threshold name that is not in ``T`` (a typo must never silently pass a check)."""


def _entry(name: str) -> tuple[Any, Status, list[str]]:
    try:
        return T[name]
    except KeyError:
        raise UnknownThreshold(name) from None


def get(name: str) -> Any:
    """Return the current value of ``name`` (a calibrated override wins over the table default)."""
    ov = _OVERRIDES.get()
    if ov and name in ov:
        _entry(name)  # still validates the name
        return ov[name]
    return _entry(name)[0]


def default(name: str) -> Any:
    """The table default, ignoring overrides."""
    return _entry(name)[0]


def status_of(name: str) -> Status:
    return _entry(name)[1]


def fm_ids_of(name: str) -> list[str]:
    return list(_entry(name)[2])


def is_tunable(name: str) -> bool:
    """Only DES and UNV values may be re-tuned or demoted (DOC, SPEC and DER values never are)."""
    return status_of(name) in TUNABLE_STATUSES


def fm_ids_for(keys: list[str]) -> list[str]:
    """Union of the failure-mode ids of several threshold keys, order-preserving."""
    seen: dict[str, None] = {}
    for k in keys:
        for i in _entry(k)[2]:
            seen.setdefault(i, None)
    return list(seen)


def describe(name: str, op: str = "") -> str:
    """Text for ``CheckResult.threshold``, e.g. ``"<= 12.0 (img.palette_de_max, DES)"``."""
    value, status, _ = _entry(name)
    cur = get(name)
    shown = f"{cur}" if cur == value else f"{cur} (default {value})"
    return f"{op} {shown} ({name}, {status})".strip()


@contextlib.contextmanager
def overrides(values: dict[str, Any]) -> Iterator[None]:
    """Apply calibrated values for the duration of a ``with`` block (used by the tuner and by tests).

    Only tunable (DES/UNV) names may be overridden.
    """
    for k in values:
        if not is_tunable(k):
            raise ValueError(f"threshold {k!r} has status {status_of(k)} and cannot be overridden")
    tok = _OVERRIDES.set({**(_OVERRIDES.get() or {}), **values})
    try:
        yield
    finally:
        _OVERRIDES.reset(tok)
