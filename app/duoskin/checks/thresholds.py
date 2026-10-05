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
    "fabric.chroma_max":          (3.0, "DES", ["CLO-13", "CLO-14"]),   # was 2 here and 3 in bible I7; one value (max chroma over all pixels) [CALIBRATE]
    "fold.mean_grey":             ((120, 136), "DES", ["CLO-14"]),
    "fold.edge_band_std_max":     (3.0, "DES", ["CLO-14"]),            # grey levels; replaces the bible I8 "mean abs diff <= 2 L*"
    "fold.panel_iou_min":         (0.98, "DES", ["CLO-14"]),           # panel outline vs the recipe mask (bible I8 A_SIL_GUIDE) [CALIBRATE]
    # ---- generic 2D assets (IMG) ----
    "img.clear_share_min":        (0.10, "DES", ["IMG-01"]),
    "img.border_frame":           (0.02, "DES", ["IMG-01"]),
    "img.haze_share_max":         (0.03, "DES", ["IMG-02"]),
    "img.halo_de_max":            (8.0, "DES", ["IMG-02"]),
    "img.margin_min":             (0.06, "DES", ["IMG-03", "ACC-03"]),
    "img.component_min_area":     (0.002, "DES", ["IMG-04"]),
    "img.palette_de_max":         (12.0, "DES", ["IMG-05", "DUO-07"]),
    "img.palette_large_reject_de":(15.0, "DES", ["IMG-05"]),
    "img.final_iou_min":          (0.92, "DES", ["IMG-06"]),
    "img.final_de_max":           (5.0, "DES", ["IMG-06"]),
    "img.reimagine_phash_max":    (6, "DES", ["IMG-08"]),
    "img.gradient_share_max":     (0.05, "DES", ["IMG-16"]),           # HARD on face lines and prints only (palette snap needs flat fills)
    "img.stroke_px_min_placed":   (2.0, "DES", ["IMG-16", "FACE-06"]), # HARD: thinnest stroke at the placed (final texel) size [CALIBRATE]
    "img.stroke_ratio_band":      ((0.7, 1.3), "DES", ["IMG-09"]),     # SOFT ranking input
    "img.shading_steps_max":      (2, "DES", ["IMG-09"]),              # SOFT [CALIBRATE]
    "img.specular_share_max":     (0.01, "DES", ["IMG-09"]),           # SOFT [CALIBRATE]
    "img.hf_energy_ratio_max":    (1.5, "DES", ["IMG-09"]),            # SOFT: profile x 1.5 [CALIBRATE]
    "img.symmetry_iou_min":       (0.90, "DES", ["IMG-10"]),
    "img.sentinel_de_min":        (40.0, "DES", ["IMG-12"]),
    "img.pasteback_ring_px":      ((4, 8), "DES", ["GEN-03"]),
    "img.pasteback_ring_de_max":  (3.0, "DES", ["GEN-03"]),
    "img.styleref_phash_max":     (10, "DES", ["IMG-15"]),
    "img.styleref_dreamsim_min":  (0.25, "DES", ["IMG-15"]),
    "svg.max_paths":              (300, "DES", ["IMG-14"]),
    "svg.border_sentinel_min":    (0.95, "DES", ["IMG-14"]),
    "ocr.rec_score_min":          (0.5, "DES", ["POL-04"]),
    "ocr.min_box_px":             (8, "DES", ["POL-04"]),
    "ocr.glyph_score_max":        (0.3, "DES", ["POL-04"]),            # A_GLYPH: a score at or above this fails [CALIBRATE]
    # ---- concept (CON) ----
    "con.body_iou_min":           (0.85, "DES", ["CON-02"]),
    "con.volume_outside_max":     (0.03, "DES", ["CON-02"]),
    "con.leak_area_max":          (0.03, "DES", ["CON-01"]),           # A_LEAK, HARD; tests partner-only colours only
    "con.partner_only_de":        (12.0, "DES", ["CON-01"]),           # partner colour is partner-only if > this from every own role colour and shared anchor [CALIBRATE]
    "con.swatch_de_warn":         (15.0, "DES", ["CON-01"]),           # own-palette adherence, SOFT (bible A_SWATCH); was HARD at 12 [CALIBRATE]
    "con.clone_proxy_dreamsim_min": (0.30, "DES", ["CON-09"]),         # concept-level clone WARNING; same start value as duo.dreamsim_clone_min [CALIBRATE]
    "con.palette_snap_de":        (10.0, "DES", ["CON-05"]),
    # ---- plan lint (PLN) ----
    "pln.anchors":                ((2, 3), "SPEC", ["PLN-03"]),   # decided workflow contract
    "pln.contrasts_min":          (5, "SPEC", ["PLN-03"]),
    "pln.face_features_diff_min": (3, "SPEC", ["PLN-03", "FACE-11"]),
    "pln.contrast_colour_de":     (15.0, "DES", ["PLN-04"]),
    "pln.contrast_value_dl":      (15.0, "DES", ["PLN-04"]),
    "pln.anchor_colour_de_max":   (6.0, "DES", ["PLN-06", "DUO-02"]),
    "pln.lash_iris_de_min":       (10.0, "DES", ["PLN-13", "FACE-10"]),
    "pln.adjacent_de_min":        (10.0, "DES", ["PLN-13"]),
    "pln.acc_per_char_warn_hard": ((3, 4), "DES", ["PLN-14"]),         # (warn at, second warning at): both SOFT; the key name is kept for stability, nothing here is HARD (APP S32)
    "pln.kit_hair_iou_warn":      (0.85, "DES", ["PLN-15"]),
    "pln.dna_char_diff_min":      (2, "SPEC", ["PLN-DNA-01"]),         # decision doc: A and B differ in >= 2 CHARACTER DNA fields
    "pln.colour_axes_max":        ((2, 1), "SPEC", ["PLN-03", "PLN-04"]),   # (default, same_club): colour axes among the >= 5 contrasts
    "pln.mirror_role_de_max":     (10.0, "DES", ["PLN-06"]),           # mirror profile: a_second ~ b_main and b_second ~ a_main (SOFT) [CALIBRATE]
    "pln.nearest_card_share":     (0.8, "UNV", ["PLN-16"]),            # share of identical DNA-card fields that counts as "very close" to a recent duo (SOFT hint) [CALIBRATE]
    # removed in v1.2: pln.accessory_cat_jaccard_max (1/3). PLN-03 now uses the (kind, category, motif) tuple rule (bible C1 #16); no number needed.
    # ---- face (FACE) ----
    "face.line_colours":          (1, "DOC", ["FACE-01"]),
    "face.lid_shade_alpha_max":   (0.35, "DES", ["FACE-01"]),
    "face.line_skin_de_min":      (20.0, "DES", ["FACE-06"]),
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
    # ---- hair / accessories / mesh ----
    "hair.guide_iou_min":         (0.98, "DES", ["HAIR-03"]),
    "hair.face_protect_frac":     (0.55, "DES", ["HAIR-03"]),
    "hair.guide_de_min":          (30.0, "DES", ["HAIR-04"]),
    "hair.tris_target":           (3600, "DES", ["HAIR-07"]),
    "hair.kit_match_min":         (0.85, "DES", ["HAIR-08"]),
    "hair.box_outside_max":       (0.005, "DES", ["HAIR-02"]),         # Gate 2 2D envelope: share of hair pixels outside the Hair box (bible I4 Gate A) [CALIBRATE]
    "hair.concept_iou_min":       (0.75, "DES", ["DUO-07"]),           # SOFT: hair silhouette vs the concept crop (bible I4 Gate A cites this key) [CALIBRATE]
    "hair.guide_texel_share_max": (0.005, "DES", ["HAIR-12"]),         # CHK-M21: guide-grey texels left in the hair texture [CALIBRATE]
    "hair.front_face_visible_min":(0.80, "DES", ["HAIR-12"]),          # CHK-M21: share of the head's front-face area visible in the front render [CALIBRATE]
    "hair.head_cut_inflate_stud": (0.02, "DES", ["HAIR-12"]),          # hair.register: head box inflation for the boolean cut [CALIBRATE]
    "view.lr_lum_diff_max":       (12.0, "DES", ["HAIR-09"]),          # L*; SOFT; was 10% here and 12 L* in bible I5 [CALIBRATE]
    "acc.view_palette_de_max":    (12.0, "DES", ["ACC-03", "ACC-15"]), # per-view palette dE to the front; was 10 in bible T1/I10 and 12 here [CALIBRATE]
    "acc.slab_convexity_min":     (0.8, "DES", ["IMG-04", "ACC-18"]),  # badge art solidity (area / convex hull); spikes inflate the bounds [CALIBRATE]
    "acc.view_height_tol":        (0.03, "DES", ["ACC-03"]),
    "acc.fill_long_side":         ((0.80, 0.85), "DES", ["ACC-03"]),
    "acc.thin_part_min_frac":     (0.02, "DES", ["ACC-03"]),
    "acc.view_iou_min":           (0.80, "DES", ["ACC-08"]),
    "acc.front_iou_min":          (0.85, "DES", ["ACC-08"]),
    "acc.mirror_margin":          (0.02, "DES", ["ACC-01"]),
    "acc.seeds":                  ((11, 29, 47), "DES", ["ACC-08"]),
    "acc.white_edge_de":          (10.0, "DES", ["ACC-03"]),       # flatten on #D9D9D9 instead of white
    "slab.thickness_min":         (0.08, "DES", ["ACC-18"]),       # RBX suggestion; Roblox publishes no number
    "slab.extent_ratio_min":      (0.03, "DES", ["ACC-18"]),
    "slab.back_mirror_phash_min": (10, "DES", ["ACC-17"]),
    "mesh.tris_max":              (3800, "DES", ["MESH-01"]),   # Roblox limit 4000 [DOC]
    "mesh.tex_warn_hard":         ((1024, 2048), "DOC", ["MESH-04"]),
    "mesh.surface_area_max":      (70.0, "UNV", ["MESH-07"]),   # UGCValidateMaxTotalSurfaceArea
    "mesh.coplanar_max_frac":     (0.15, "UNV", ["MESH-11"]),
    "mesh.center_offset_max":     (1.0, "UNV", ["MESH-11"]),
    "mesh.scale_min":             (0.01, "UNV", ["MESH-11"]),
    "mesh.components_max":        (10, "UNV", ["MESH-09"]),
    "mesh.shells_warn":           (8, "DES", ["MESH-09"]),
    "mesh.normals_out_min":       (0.99, "DES", ["MESH-08"]),
    "mesh.thickness_min":         (0.05, "DES", ["MESH-08"]),
    "mesh.sparse_cover_warn_fail":((0.50, 0.30), "UNV", ["MESH-10"]),
    "mesh.orient_iou_min":        (0.80, "DES", ["MESH-12"]),
    "mesh.clip_depth_max":        (0.02, "DES", ["MESH-16", "HAIR-06"]),
    "mesh.gap_max":               (0.10, "DES", ["MESH-16"]),
    # ---- duo ----
    "duo.dreamsim_clone_min":     (0.30, "DES", ["DUO-01"]),   # SUM value, tune on labels; the HARD lower edge
    "duo.degraded_phash_clone_max": (10, "DES", ["DUO-01"]),           # degraded mode (a): mean A-vs-B pHash Hamming per side <= this trips [CALIBRATE]
    "duo.degraded_palette_overlap_max": (0.50, "DES", ["DUO-01"]),     # degraded mode (b): overlap share > this trips [CALIBRATE]
    "duo.degraded_spec_dist_min": (0.40, "DES", ["DUO-01"]),           # degraded mode (c): differing share of the 22 compared fields listed in DUO-01 < this trips; all three must trip to fail [CALIBRATE]
    "duo.strangers_dreamsim_max": (0.65, "DES", ["DUO-02"]),           # SOFT strangers upper edge; tuned on "strangers" drill labels [CALIBRATE]
    "duo.same_world_style_max":   (0.45, "DES", ["DUO-02"]),           # SOFT same-world baseline: luminance-only front renders, DreamSim distance [CALIBRATE]
    "duo.anchor_area_min":        (0.01, "DES", ["DUO-02"]),
    "duo.thumb_height_px":        ((120, 150), "DES", ["DUO-03"]),
    "duo.silhouette_iou_cold":    (0.85, "DES", ["DUO-04"]),           # cold start for the 95th-percentile warning (< 5 approved duos) [CALIBRATE]
    "duo.interpenetration_max":   (0.005, "DES", ["DUO-06"]),
    "duo.nearest_duo_dreamsim_max": (0.15, "DES", ["DUO-10"]),         # "very close" to a past duo (SOFT) [CALIBRATE]
    "duo.nearest_duo_phash_max":  (8, "DES", ["DUO-10"]),              # the same, while degraded [CALIBRATE]
    "taste.acc_min_px":           (8, "DES", ["DUO-04", "PLN-14"]),    # SOFT: accessory visible at phone size (px, area-downscaled) [CALIBRATE]
    "taste.kmeans_k":             (5, "DES", ["DUO-03"]),              # clusters for the phone-size top colours (k-means in CIELAB) [CALIBRATE]
    "taste.top_n":                (2, "SPEC", ["DUO-03"]),             # the planned main colour must be among the top N clusters (APP_SPEC 3.4)
    "taste.plan_ratio_share_off": (15, "DES", ["DUO-03"]),             # TASTE_RATIO (SOFT): share points a built colour may be off its declared colour plan [CALIBRATE]
    "taste.layout_ari_warn":      (0.80, "DES", ["DUO-11"]),           # SOFT: A-vs-B colour-block layout, adjusted Rand index above this warns (build, label maps) [CALIBRATE]
    "dreamsim.fixture_tol":       (0.01, "DES", ["DUO-01", "IMG-15"]), # CHK-S14: the fixture pair must return its expected distance +- this [CALIBRATE]
    "pol.ref_dreamsim_min":       (0.25, "DES", ["POL-02"]),
    "pol.ref_phash_max":          (10, "DES", ["POL-02"]),
    # ---- prompt lint (PRM) ----
    "prm.must_max":               (5, "SPEC", ["PRM-01"]),             # decision doc: <= 5 constraints per call
    "prm.dna_fields_max":         (2, "SPEC", ["PRM-01", "PRM-10"]),
    "prm.max_chars_excl_style":   (1500, "DES", ["PRM-01"]),           # bible 0.4 / APP 10.0: excluding the verbatim STYLE block [CALIBRATE]
    "prm.max_chars_total":        (2200, "DES", ["PRM-01"]),   # [CALIBRATE]
    "prm.exclude_nouns_max":      (10, "DES", ["PRM-01"]),   # [CALIBRATE]
    # ---- calibration and learning loop (calib) ----
    "calib.percentile_min_duos":  (5, "SPEC", ["DUO-04", "DUO-10"]),   # percentile warnings use *_cold keys below this many approved duos
    "calib.target_labels":        (200, "SPEC", ["ENG-10"]),           # about 200 labels before [DES] values are re-tuned (T10)
    "calib.tail_percentile":      (5, "SPEC", ["ENG-10"]),             # bad-tail bound = about the 5th percentile of approved duos
    "calib.drill_share_max":      (0.25, "SPEC", ["ENG-10"]),          # drill labels <= 25% of the calibration set
    "calib.demote_fire_rate":     (0.30, "SPEC", ["ENG-10"]),          # a check firing on more than this share of duos goes back to SOFT
    "calib.hard_reject_rate_max": (0.10, "SPEC", ["ENG-10"]),          # all HARD checks together, on duos the user approved
    "calib.registry_reject_alert":(0.15, "DES", ["PLN-16"]),           # registry or plan-lint reject rate per 10 duos: "grow the kits" [CALIBRATE]
    "calib.structure_collapse_share": (0.35, "SPEC", ["PLN-STR-01"]),  # one pair structure above this share after >= 10 duos
    "calib.wildcard_pick_rate":   (0.30, "DES", ["PLN-08"]),           # suggest more novelty weight when the wildcard is picked this often [CALIBRATE]
    "calib.first_try_approval_min": (0.50, "DES", ["CON-07"]),         # Gate 1 first-try approval below this triggers the 12-concepts review [CALIBRATE]
    "calib.variety_drop_max":     (0.05, "SPEC", ["ENG-08"]),          # variety guard on the same 40 briefs
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

    # ---- keys added by the imaging track (not in FAILURE_MODES §4.1): algorithm parameters of the code checks, so that check
    # ---- code holds no number other than 0, 1, -1, 2, indexes and slice bounds (the no-literals rule). DES/DOC as marked.
    "num.eps":                    (1e-9, "DOC", ["IMG-01"]),         # division guard
    "num.far_de":                 (99.0, "DOC", ["CON-05"]),         # stand-in distance for an empty zone
    "runner.evidence_max":        (600, "DES", ["ENG-09"]),          # CheckResult.evidence is kept short
    "img.connectivity":           (8, "DOC", ["IMG-04"]),            # connected components use 8-connectivity
    "img.morph_kernel":           (3, "DOC", ["IMG-04"]),            # 3x3 structuring element
    "img.alpha_binarize":         (128, "DES", ["IMG-02"]),          # alpha >= this is visible; cel-art binarisation (bible 2.5)
    "img.palette_min_cluster":    (0.02, "DES", ["IMG-05"]),         # bible A_PALETTE: clusters >= 2% of the area count
    "img.palette_large_share":    (0.05, "DES", ["IMG-05"]),         # a cluster >= 5% of the foreground is "large"
    "img.palette_k_extra":        (2, "DES", ["IMG-05"]),            # k-means k = allowed colours + this (min 3, max 10)
    "img.palette_k_bounds":       ((3, 10), "DES", ["IMG-05"]),
    "img.checker_peak_ratio":     (0.12, "DES", ["IMG-01"]),         # painted checkerboard: share of the spectrum in the two diagonal peaks
    "img.checker_block_px":       (256, "DES", ["IMG-01"]),          # FFT tile size
    "img.checker_min_block_px":   (128, "DES", ["IMG-01"]),
    "img.checker_k_min":          (4, "DES", ["IMG-01"]),            # lowest FFT frequency index that counts
    "img.checker_max_blocks":     (24, "DES", ["IMG-01"]),
    "img.checker_opaque_frac":    (0.98, "DES", ["IMG-01"]),         # a tile must be this opaque to be tested
    "img.checker_min_std":        (3.0, "DES", ["IMG-01"]),          # a flat tile has no pattern
    "img.checker_diag_tol":       (0.2, "DES", ["IMG-01"]),          # the fundamental must be on the diagonal within this
    "img.checker_axis_energy":    (0.2, "DES", ["IMG-01"]),          # ...with at most this much energy on the axes
    "img.halo_ring_px":           (3, "DES", ["IMG-02"]),
    "img.edge_lum_ratio_min":     (0.90, "DES", ["IMG-13"]),         # edge luminance >= 90% of interior neighbours
    "img.stroke_thin_tol":        (0.10, "DES", ["IMG-16"]),         # share of the skeleton allowed to be thinner than the minimum
    "img.stroke_tip_px":          (3, "DES", ["IMG-16"]),            # skeleton ends this close to a tip are ignored (tapers)
    "img.stroke_cap_px":          (64, "DES", ["IMG-16"]),
    "img.stroke_cap_margin_px":   (4, "DES", ["IMG-16"]),
    "img.min_interior_px":        (4, "DES", ["FACE-01"]),           # fewer interior pixels: a hair-thin line, use alpha >= hairline
    "img.hairline_alpha":         (200, "DES", ["FACE-01"]),
    "img.single_colour_tol":      (2, "DES", ["FACE-01"]),           # RGB per channel
    "img.single_colour_de":       (3.0, "DES", ["FACE-01"]),         # line colour vs the spec colour
    "img.gradient_step_min":      (0.5, "DES", ["IMG-16"]),          # CIE76 step range that counts as a smooth ramp
    "img.gradient_step_max":      (2.0, "DES", ["IMG-16"]),
    "img.badge_outline_min":      (0.03, "DES", ["ACC-18"]),         # I6 A_STROKE: outline >= 3% of the width
    "img.highlight_max_blobs":    (0, "SPEC", ["FACE-04"]),          # A_HIGHLIGHT: no white blobs inside the iris
    "img.highlight_blob_frac":    (0.15, "DES", ["FACE-04"]),        # a white blob smaller than this share of the iris is a highlight
    "img.white_l_min":            (88.0, "DES", ["FACE-04"]),
    "img.white_chroma_max":       (12.0, "DES", ["FACE-04"]),
    "img.specular_l_min":         (92.0, "DES", ["IMG-09"]),
    "img.specular_chroma_max":    (10.0, "DES", ["IMG-09"]),
    "img.hf_cutoff":              (0.25, "DES", ["IMG-09"]),         # spatial frequency above which energy counts as high-frequency
    "img.guide_left_max":         (0.005, "DES", ["HAIR-04"]),       # A_GUIDE_LEFT: guide-grey pixels < 0.5%
    "img.guide_left_de":          (5.0, "DES", ["HAIR-04"]),
    "img.views_height_tol":       (0.03, "DES", ["ACC-03"]),         # A_VIEWS heights (same as acc.view_height_tol)
    "img.views_ground_tol":       (0.01, "DES", ["ACC-03"]),
    "img.views_centre_tol":       (0.02, "DES", ["ACC-03"]),
    "img.drift_palette_k":        (6, "DES", ["IMG-06"]),
    "img.drift_merge_de":         (4.0, "DES", ["IMG-06"]),
    "img.dominant_k":             (3, "DES", ["CON-05"]),
    "img.dominant_merge_de":      (8.0, "DES", ["CON-05"]),
    "svg.matte_edge_err_max":     (6, "DES", ["IMG-14"]),            # two-pass matte edge error, /255
    "svg.snap_de_max":            (15.0, "DES", ["IMG-14"]),         # a fill farther than this from the palette is rejected
    "sim.hash_size":              (8, "DOC", ["FACE-11"]),           # pHash / dHash side: 8x8 = 64 bits
    "sim.hash_bits":              (64, "DOC", ["FACE-11"]),
    "sim.dreamsim_input_px":      (224, "DOC", ["DUO-01"]),
    "sim.palette_k":              (6, "DES", ["DUO-01"]),
    "sim.palette_merge_de":       (4.0, "DES", ["DUO-01"]),
    "sim.palette_overlap_de":     (12.0, "DES", ["DUO-01"]),         # clusters within this count as shared (DUO-01 (b))
    "face.blush_l_max":           (45.0, "DES", ["FACE-07"]),        # blush L* <= 45
    "face.blush_alpha_max":       (0.35, "DES", ["FACE-07"]),
    "face.line_edge_ring_de_max": (8.0, "DES", ["FACE-06"]),
    "face.lid_cover_min":         (0.99, "DES", ["FACE-03"]),        # 2D: the closed-lid layer covers the sclera polygon
    "face.blink_iris_px_max":     (0, "SPEC", ["FACE-03"]),          # iris pixels in the blink render
    "face.zone_inside_min":       (1.0, "DES", ["FACE-02"]),         # 2D: share of feature pixels inside their canvas zones
    "face.catchlight_tol_px":     (1.0, "DES", ["FACE-04"]),
    "face.neck_seam_de_max":      (2.0, "DES", ["FACE-12"]),         # CHK-B09
    "face.skin_alpha_zero_min":   (0.95, "DES", ["FACE-12"]),        # >= 95% of the skin zone of the head texture is alpha 0
    "face.mouth_inner_min_alpha": (0.99, "DES", ["FACE-14"]),        # open-mouth interior painted
    "uv.warp_density":            ((2, 4), "DES", ["FACE-05"]),      # LUT oversample before the downsample
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


tv = get   # the name FAILURE_MODES §5.3 uses: "the only way check code reads a number"


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
