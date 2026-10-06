"""Perceptual similarity: pHash / dHash, palette overlap, spec distance, optional DreamSim (ONNX), registry and clone band.

DreamSim runs only if ``DATA\\models\\dreamsim.onnx`` exists and ``onnxruntime`` is importable (``load_dreamsim``). Without it every
check that needs DreamSim runs in a clearly labelled **degraded** mode (``mode == "degraded"``): pHash only for the registries and for
reference leakage, and for the clone band the three-metric rule of FAILURE_MODES DUO-01. Nothing here ever pretends to have run
DreamSim.

Registry rule (issue "one rule", FAILURE_MODES FACE-11): a candidate is blocked when its decoded-pixel SHA-256 equals **any** registered
row of the same kind (forever), or - for the assembled ``face_canvas`` and the whole ``print`` only - when ``pHash <= 8`` **or**
``DreamSim < 0.15`` against a row inside the sliding window (last ~30 duos) or flagged ``listed``.
"""
from __future__ import annotations

import importlib.util
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import imagehash
import numpy as np
from PIL import Image

from duoskin.checks import thresholds as TH
from duoskin.checks.model import CheckResult
from duoskin.checks.runner import CheckUnavailable, build_result, fail_closed
from duoskin.imaging import checks as C
from duoskin.imaging import files as F
from duoskin.imaging import palette as P

NEAR_DUP_KINDS = ("face_canvas", "print")
SIDES = ("front", "back", "left", "right")
Mode = Literal["dreamsim", "degraded"]


# ------------------------------------------------------------------ hashes
def _flat_rgb(im: Image.Image, crop_to_subject: bool = False) -> Image.Image:
    rgba = im.convert("RGBA")
    if crop_to_subject:
        a = np.asarray(rgba)[..., 3]
        ys, xs = np.nonzero(C.visible(a))
        if len(ys):
            rgba = rgba.crop((int(xs.min()), int(ys.min()), int(xs.max()) + 1, int(ys.max()) + 1))
    return F.flatten(rgba, (255, 255, 255))


def _bits_to_int(h: imagehash.ImageHash) -> int:
    value = 0
    for bit in h.hash.flatten():
        value = (value << 1) | int(bit)
    return value


def phash(im: Image.Image, *, crop_to_subject: bool = False) -> int:
    """64-bit perceptual hash (DCT) of the image composited on white, as an int."""
    return _bits_to_int(imagehash.phash(_flat_rgb(im, crop_to_subject), hash_size=int(TH.get("sim.hash_size"))))


def dhash(im: Image.Image, *, crop_to_subject: bool = False) -> int:
    """64-bit difference hash as an int."""
    return _bits_to_int(imagehash.dhash(_flat_rgb(im, crop_to_subject), hash_size=int(TH.get("sim.hash_size"))))


def hamming(a: int, b: int) -> int:
    return (a ^ b).bit_count()


def hash_hex(h: int) -> str:
    return f"{h:016x}"


# ------------------------------------------------------------------ DreamSim (optional, ONNX)
class DreamSimUnavailable(RuntimeError):
    """The ONNX model or onnxruntime is missing, or the file is not a usable DreamSim export."""


class DreamSimModel:
    """Thin wrapper around an ONNX DreamSim export on onnxruntime.

    Two supported exports: a **pair** model (two image inputs, one distance output) and an **embedding** model (one image input,
    one embedding output; the distance is ``1 - cosine``). The input is ``(1, 3, 224, 224)`` float32 in ``[0, 1]``.
    The export itself is made once off the user's PC (FAILURE_MODES X19); ``verify_fixture`` is the CHK-S14 probe.
    """

    def __init__(self, session: Any):
        self._s = session
        self._inputs = [i.name for i in session.get_inputs()]
        if len(self._inputs) not in (1, 2):
            raise DreamSimUnavailable(f"unexpected input count {len(self._inputs)}")
        self.kind: Literal["pair", "embedding"] = "pair" if len(self._inputs) == 2 else "embedding"

    @classmethod
    def load(cls, path: str | Path) -> DreamSimModel:
        p = Path(path)
        if not p.is_file():
            raise DreamSimUnavailable(f"{p} does not exist")
        if importlib.util.find_spec("onnxruntime") is None:
            raise DreamSimUnavailable("onnxruntime is not installed")
        try:
            import onnxruntime as ort

            sess = ort.InferenceSession(str(p), providers=["CPUExecutionProvider"])
        except Exception as e:
            raise DreamSimUnavailable(f"could not load {p.name}: {type(e).__name__}: {e}") from e
        return cls(sess)

    def _prep(self, im: Image.Image) -> np.ndarray:
        side = int(TH.get("sim.dreamsim_input_px"))
        rgb = _flat_rgb(im).resize((side, side), Image.Resampling.BICUBIC)
        return (np.asarray(rgb, dtype=np.float32) / 255.0).transpose(2, 0, 1)[None, ...]

    def embed(self, im: Image.Image) -> np.ndarray:
        if self.kind != "embedding":
            raise DreamSimUnavailable("this export has no standalone embedding output")
        out = self._s.run(None, {self._inputs[0]: self._prep(im)})[0]
        return np.asarray(out, dtype=np.float64).reshape(-1)

    @staticmethod
    def cosine_distance(a: np.ndarray, b: np.ndarray) -> float:
        na, nb = float(np.linalg.norm(a)), float(np.linalg.norm(b))
        return 1.0 if na == 0 or nb == 0 else float(1.0 - float(a @ b) / (na * nb))

    def distance(self, a: Image.Image, b: Image.Image) -> float:
        if self.kind == "pair":
            out = self._s.run(None, {self._inputs[0]: self._prep(a), self._inputs[1]: self._prep(b)})[0]
            return float(np.asarray(out).reshape(-1)[0])
        return self.cosine_distance(self.embed(a), self.embed(b))


def load_dreamsim(path: str | Path | None) -> DreamSimModel | None:
    """The model, or ``None`` when it cannot be used (degraded mode). Never raises."""
    if path is None:
        return None
    try:
        return DreamSimModel.load(path)
    except DreamSimUnavailable:
        return None


def verify_fixture(model: DreamSimModel | None, img_a: Image.Image, img_b: Image.Image, expected: float, *,
                   subject_sha: str = "") -> CheckResult:
    """CHK-S14 (SOFT): the model loads and a fixture pair returns the expected distance within ``dreamsim.fixture_tol``."""
    if model is None:
        return build_result("CHK-S14", passed=False, subject_sha=subject_sha, metric="dreamsim_present", evidence="dreamsim.onnx not available: "
                            "the clone band runs in degraded mode")
    try:
        d = model.distance(img_a, img_b)
    except Exception as e:  # noqa: BLE001
        return fail_closed("CHK-S14", f"{type(e).__name__}: {e}", subject_sha)
    tol = float(TH.get("dreamsim.fixture_tol"))
    return build_result("CHK-S14", passed=abs(d - expected) <= tol, subject_sha=subject_sha, metric="fixture_distance", value=d,
                        threshold=f"{expected} +- {tol} (dreamsim.fixture_tol, DES)", evidence=f"distance {d:.4f}")


# ------------------------------------------------------------------ registry (exact forever, near-duplicates in the window)
@dataclass
class RegistryRow:
    """One registered face canvas, face part or print (the ``registry`` table, APP_SPEC §6.13)."""

    kind: str
    pixel_sha: str
    phash: int | None = None
    duo_seq: int = 0
    listed: bool = False
    embedding: Sequence[float] | None = None
    asset_id: str = ""


@dataclass
class RegistryHit:
    row: RegistryRow
    reason: Literal["exact", "near"]
    phash_distance: int | None = None
    dreamsim_distance: float | None = None


def registry_fingerprint(im: Image.Image, model: DreamSimModel | None = None) -> dict[str, Any]:
    """The values to store when a face or print is registered: ``pixel_sha``, ``phash`` and (with DreamSim) ``embedding``."""
    out: dict[str, Any] = {"pixel_sha": F.pixel_sha(im), "phash": phash(im)}
    if model is not None and model.kind == "embedding":
        out["embedding"] = model.embed(im).tolist()
    return out


def find_registry_hits(kind: str, candidate: Image.Image, rows: Iterable[RegistryRow], *, current_duo_seq: int = 0, window: int | None = None,
                       model: DreamSimModel | None = None) -> tuple[list[RegistryHit], Mode]:
    """All exact and near-duplicate hits of ``candidate`` among ``rows`` (and the mode the near test ran in)."""
    win = int(TH.get("face.registry_window_duos")) if window is None else window
    ph_max = int(TH.get("face.registry_phash_max"))
    ds_min = float(TH.get("face.registry_dreamsim_min"))
    sha = F.pixel_sha(candidate)
    hits: list[RegistryHit] = []
    cand_hash: int | None = None
    cand_emb: np.ndarray | None = None
    used_dreamsim = False
    for row in rows:
        if row.kind != kind:
            continue
        if row.pixel_sha == sha:
            hits.append(RegistryHit(row, "exact"))
            continue
        if kind not in NEAR_DUP_KINDS:
            continue
        if not (row.listed or row.duo_seq > current_duo_seq - win):
            continue
        if cand_hash is None:
            cand_hash = phash(candidate)
        d_h = hamming(cand_hash, row.phash) if row.phash is not None else None
        d_s: float | None = None
        if model is not None and model.kind == "embedding" and row.embedding is not None:
            if cand_emb is None:
                cand_emb = model.embed(candidate)
            d_s = DreamSimModel.cosine_distance(cand_emb, np.asarray(row.embedding, dtype=np.float64))
            used_dreamsim = True
        elif model is not None and model.kind == "pair":
            raise CheckUnavailable("a pair-style DreamSim export cannot compare against stored rows; store images or use an embedding export")
        if (d_h is not None and d_h <= ph_max) or (d_s is not None and d_s < ds_min):
            hits.append(RegistryHit(row, "near", d_h, d_s))
    return hits, ("dreamsim" if used_dreamsim else "degraded")


def registry_check(kind: str, candidate: Image.Image, rows: Iterable[RegistryRow] | None = None, *, current_duo_seq: int = 0,
                   window: int | None = None, model: DreamSimModel | None = None, subject_sha: str = "") -> CheckResult:
    """A_REGISTRY (HARD, class registry): exact reuse of a registered file is blocked forever; near-duplicates of the assembled
    ``face_canvas`` and of the whole ``print`` are blocked inside the sliding window (pHash <= 8 or DreamSim < 0.15).

    ``rows=None`` means the registry could not be read: the check fails closed. An empty list passes.
    """
    if rows is None:
        return fail_closed("A_REGISTRY", "the registry rows were not provided", subject_sha)
    try:
        hits, mode = find_registry_hits(kind, candidate, rows, current_duo_seq=current_duo_seq, window=window, model=model)
    except CheckUnavailable as e:
        return fail_closed("A_REGISTRY", f"unavailable: {e}", subject_sha)
    exact = [h for h in hits if h.reason == "exact"]
    near = [h for h in hits if h.reason == "near"]
    thr = (f"exact sha forever; near: phash <= {TH.get('face.registry_phash_max')} or dreamsim < {TH.get('face.registry_dreamsim_min')} "
           f"within {TH.get('face.registry_window_duos')} duos (face.registry_*, DES); mode={mode}")
    if exact:
        return build_result("A_REGISTRY", passed=False, subject_sha=subject_sha, metric="exact_sha", value=1.0, threshold=thr,
                            evidence=f"exact reuse of {exact[0].row.asset_id or exact[0].row.pixel_sha[:12]} (blocked forever)",
                            fix_hint="regenerate")
    if near:
        h = near[0]
        ev = f"near-duplicate of {h.row.asset_id or h.row.pixel_sha[:12]} (duo {h.row.duo_seq}): phash distance {h.phash_distance}"
        if h.dreamsim_distance is not None:
            ev += f", dreamsim {h.dreamsim_distance:.3f}"
        return build_result("A_REGISTRY", passed=False, subject_sha=subject_sha, metric="phash_distance",
                            value=None if h.phash_distance is None else float(h.phash_distance), threshold=thr,
                            evidence=f"{ev}; mode={mode}", fix_hint="regenerate")
    return build_result("A_REGISTRY", passed=True, subject_sha=subject_sha, metric="registry_hits", value=0.0, threshold=thr,
                        evidence=f"no hit among {sum(1 for r in rows if r.kind == kind)} rows; mode={mode}")


def dedupe_check(candidate: Image.Image, rejected: Iterable[int | Image.Image], *, subject_sha: str = "") -> CheckResult:
    """A_PHASH (CHK-A14, SOFT): a Reimagine draft within pHash distance 6 of a draft the user rejected is dropped and drawn again."""
    h = phash(candidate)
    lim = int(TH.get("img.reimagine_phash_max"))
    best = None
    for r in rejected:
        rh = r if isinstance(r, int) else phash(r)
        d = hamming(h, rh)
        best = d if best is None else min(best, d)
    ok = best is None or best > lim
    return build_result("A_PHASH", passed=ok, subject_sha=subject_sha, metric="phash_distance_min", value=None if best is None else float(best),
                        threshold=TH.describe("img.reimagine_phash_max", ">"), evidence="no rejected drafts" if best is None else f"closest rejected draft: {best}")


def check_reference_leakage(outputs: Sequence[Image.Image], references: Mapping[str, Image.Image], *, own_keys: Iterable[str] = (),
                            user_reference_keys: Iterable[str] = (), toggle_on: bool = False, model: DreamSimModel | None = None,
                            subject_sha: str = "") -> CheckResult:
    """A_REFLEAK (CHK-A15 / IMG-15, HARD): the output (and its component crops) must not copy a house-style or per-duo reference.

    pHash distance <= 10, or DreamSim < 0.25, against any reference crop that is not the asset's own concept crop (``own_keys``) fails.
    The user's own reference crops (``user_reference_keys``) are compared only when the reference-similarity toggle is on.
    Without DreamSim only the pHash form runs (``mode=degraded``).
    """
    own = set(own_keys)
    user = set(user_reference_keys)
    ph_max = int(TH.get("img.styleref_phash_max"))
    ds_min = float(TH.get("img.styleref_dreamsim_min"))
    mode: Mode = "dreamsim" if model is not None else "degraded"
    worst: tuple[str, int, float | None] | None = None
    for key, ref in references.items():
        if key in own or (key in user and not toggle_on):
            continue
        rh = phash(ref)
        for out in outputs:
            d_h = hamming(phash(out), rh)
            d_s = None
            if model is not None:
                try:
                    d_s = model.distance(out, ref)
                except Exception as e:  # noqa: BLE001
                    return fail_closed("A_REFLEAK", f"dreamsim failed: {type(e).__name__}: {e}", subject_sha)
            if (d_h <= ph_max or (d_s is not None and d_s < ds_min)) and (worst is None or d_h < worst[1]):
                worst = (key, d_h, d_s)
    thr = f"phash > {ph_max} and dreamsim >= {ds_min} (img.styleref_*, DES); mode={mode}"
    if worst:
        ev = f"output matches reference '{worst[0]}': phash distance {worst[1]}" + (f", dreamsim {worst[2]:.3f}" if worst[2] is not None else "") + f"; mode={mode}"
        return build_result("A_REFLEAK", passed=False, subject_sha=subject_sha, metric="phash_distance", value=float(worst[1]), threshold=thr,
                            evidence=ev, fix_hint="regenerate")
    return build_result("A_REFLEAK", passed=True, subject_sha=subject_sha, metric="phash_distance", threshold=thr,
                        evidence=f"no reference leak among {len(references)} reference crop(s); mode={mode}")


# ------------------------------------------------------------------ spec distance and the clone band
# The 22 compared fields of PLN-03 (per character): CHARACTER DNA, hair, face grammar and garment cut.
PLN03_FIELDS: tuple[str, ...] = (
    "dna.shape_language", "dna.colour_plan", "dna.focal_location", "dna.motif_object", "dna.accessory_style", "dna.energy",
    "hair.kit_style_id", "hair.fringe_id", "hair.back_id",
    "face.eye_shape", "face.iris_style", "face.highlight_style", "face.lash_style", "face.brow_style", "face.mouth_style", "face.cheek_mark",
    "top.recipe_id", "top.sleeve", "top.hem", "top.neckline", "bottom.recipe_id", "bottom.leg",
)


def _get(d: Mapping[str, Any], path: str) -> Any:
    cur: Any = d
    for part in path.split("."):
        if not isinstance(cur, Mapping) or part not in cur:
            return None
        cur = cur[part]
    return cur


def _norm(v: Any) -> Any:
    return " ".join(v.lower().split()) if isinstance(v, str) else v


def spec_distance(a: Mapping[str, Any], b: Mapping[str, Any], fields: Sequence[str] = PLN03_FIELDS) -> float:
    """Share of compared fields on which two character specs differ (text compared case- and space-insensitively).

    A field present in neither character is not compared; one present in only one counts as different. Empty input gives 0.0.
    """
    compared = differing = 0
    for f in fields:
        va, vb = _norm(_get(a, f)), _norm(_get(b, f))
        if va is None and vb is None:
            continue
        compared += 1
        differing += int(va != vb)
    return differing / compared if compared else 0.0


@dataclass
class CloneBandResult:
    mode: Mode
    passed: bool
    metrics: dict[str, float] = field(default_factory=dict)
    per_side: dict[str, float] = field(default_factory=dict)
    trips: list[str] = field(default_factory=list)       # degraded mode: which of (a) (b) (c) tripped
    warnings: list[str] = field(default_factory=list)
    label: str = ""                                       # "clone check degraded" when the degraded rules ran


DEGRADED_METRICS = ("phash", "palette_overlap", "spec_distance")
_STAGE_KEY = {"concept": "con.clone_proxy_dreamsim_min", "duo": "duo.dreamsim_clone_min"}


def _palette_for(im: Image.Image, bg_hex: str | None) -> list[P.ColourCluster]:
    return P.extract_palette(im, k=int(TH.get("sim.palette_k")), exclude_hex=[bg_hex] if bg_hex else (), merge_de=float(TH.get("sim.palette_merge_de")))


def clone_band_metrics(a_views: Mapping[str, Image.Image], b_views: Mapping[str, Image.Image], *, model: DreamSimModel | None = None,
                       spec_distance_value: float | None = None, bg_hex: str | None = None,
                       stage: Literal["concept", "duo"] = "duo") -> CloneBandResult:
    """Clone band between character A and B over the sides present in both view sets (FAILURE_MODES DUO-01, CON-09).

    **DreamSim mode**: the mean A-vs-B distance over the sides must be at least the stage's edge: ``duo.dreamsim_clone_min`` (0.30,
    **HARD** lower edge on the 4-side renders) at the duo stage, ``con.clone_proxy_dreamsim_min`` (a **SOFT** warning on the concept
    figure crops) at the concept stage. There is no Gate 2 stage.

    **Degraded mode** (no model, duo stage): three metrics on the same views - (a) mean pHash Hamming distance per side <=
    ``duo.degraded_phash_clone_max``, (b) palette overlap > ``duo.degraded_palette_overlap_max``, (c) spec distance <
    ``duo.degraded_spec_dist_min``. The pair is a degraded clone, and the lower edge fails, **only when all three trip**; one or two trips
    are warnings. The result is always labelled ``clone check degraded``.
    """
    sides = [s for s in SIDES if s in a_views and s in b_views] or sorted(set(a_views) & set(b_views))
    if not sides:
        raise CheckUnavailable("no matching sides between A and B")
    if model is not None:
        per = {s: model.distance(a_views[s], b_views[s]) for s in sides}
        mean = float(np.mean(list(per.values())))
        edge = float(TH.get(_STAGE_KEY[stage]))
        return CloneBandResult("dreamsim", mean >= edge, {"dreamsim_mean": mean, "lower_edge": edge}, per)
    ph = {s: float(hamming(phash(a_views[s], crop_to_subject=True), phash(b_views[s], crop_to_subject=True))) for s in sides}
    ph_mean = float(np.mean(list(ph.values())))
    overlap_de = float(TH.get("sim.palette_overlap_de"))
    overlaps = [P.palette_overlap(_palette_for(a_views[s], bg_hex), _palette_for(b_views[s], bg_hex), overlap_de) for s in sides]
    ov = float(np.mean(overlaps))
    trips: list[str] = []
    if ph_mean <= float(TH.get("duo.degraded_phash_clone_max")):
        trips.append(DEGRADED_METRICS[0])
    if ov > float(TH.get("duo.degraded_palette_overlap_max")):
        trips.append(DEGRADED_METRICS[1])
    metrics = {"phash_mean": ph_mean, "palette_overlap": ov}
    if spec_distance_value is None:
        raise CheckUnavailable("degraded clone check needs the A-vs-B spec distance")
    metrics["spec_distance"] = float(spec_distance_value)
    if spec_distance_value < float(TH.get("duo.degraded_spec_dist_min")):
        trips.append(DEGRADED_METRICS[2])
    clone = len(trips) == len(DEGRADED_METRICS)
    warns = [f"degraded clone metric tripped: {t}" for t in trips] if trips and not clone else []
    return CloneBandResult("degraded", not clone, metrics, ph, trips, warns, "clone check degraded")


_STAGE_CHECK_ID = {"concept": "CHK-G1-11", "duo": "CHK-D02"}


def check_clone_band(a_views: Mapping[str, Image.Image], b_views: Mapping[str, Image.Image], *, model: DreamSimModel | None = None,
                     spec_distance_value: float | None = None, bg_hex: str | None = None,
                     stage: Literal["concept", "duo"] = "duo", check_id: str | None = None, subject_sha: str = "") -> CheckResult:
    """The clone band as a ``CheckResult``: ``CHK-D02`` at the duo stage (HARD, class clone_lower_edge) and ``CHK-G1-11`` at the concept
    stage (SOFT warning, only shown while DreamSim exists). The evidence carries the mode and its metrics; a degraded run is labelled
    ``clone check degraded`` and may block only when all three metrics trip (APP_SPEC S33)."""
    cid = check_id or _STAGE_CHECK_ID[stage]
    if stage == "concept" and model is None:
        return build_result(cid, passed=True, subject_sha=subject_sha, metric="dreamsim_present", evidence="not shown while DreamSim is absent")
    try:
        r = clone_band_metrics(a_views, b_views, model=model, spec_distance_value=spec_distance_value, bg_hex=bg_hex, stage=stage)
    except CheckUnavailable as e:
        return fail_closed(cid, str(e), subject_sha)
    metrics = ", ".join(f"{k}={v:.3f}" for k, v in r.metrics.items())
    if r.mode == "dreamsim":
        edge = r.metrics["lower_edge"]
        return build_result(cid, passed=r.passed, subject_sha=subject_sha, metric="dreamsim_mean", value=r.metrics["dreamsim_mean"],
                            threshold=f">= {edge} ({_STAGE_KEY[stage]}, DES)", evidence=f"mode=dreamsim; {metrics}", fix_hint="revise_plan")
    ev = f"mode=degraded ({r.label}); {metrics}; tripped: {','.join(r.trips) or 'none'}" + (f"; {'; '.join(r.warnings)}" if r.warnings else "")
    return build_result(cid, passed=r.passed, subject_sha=subject_sha, metric="degraded_trips", value=float(len(r.trips)),
                        threshold=f"fails only when all {len(DEGRADED_METRICS)} trip (duo.degraded_*, DES)", evidence=ev, fix_hint="revise_plan")


# ------------------------------------------------------------------ cross-duo memory (DUO-10)
def memory_vector(views: Mapping[str, Image.Image], model: DreamSimModel | None = None) -> dict[str, Any]:
    """What is stored per approved duo for the nearest-past-duo warning: DreamSim embeddings, or the degraded pHash vector."""
    sides = [s for s in SIDES if s in views]
    if model is not None and model.kind == "embedding":
        return {"mode": "dreamsim", "sides": sides, "emb": [model.embed(views[s]).tolist() for s in sides]}
    return {"mode": "degraded", "sides": sides, "phash": [phash(views[s], crop_to_subject=True) for s in sides]}


def memory_distance(a: Mapping[str, Any], b: Mapping[str, Any]) -> float | None:
    """Distance between two stored vectors (``None`` when the modes differ). DreamSim: mean cosine distance over common sides;
    degraded: mean normalised Hamming distance (0..1)."""
    if a.get("mode") != b.get("mode"):
        return None
    common = [s for s in a["sides"] if s in b["sides"]]
    if not common:
        return None
    if a["mode"] == "dreamsim":
        return float(np.mean([DreamSimModel.cosine_distance(np.asarray(a["emb"][a["sides"].index(s)]), np.asarray(b["emb"][b["sides"].index(s)]))
                              for s in common]))
    return float(np.mean([hamming(a["phash"][a["sides"].index(s)], b["phash"][b["sides"].index(s)]) / float(TH.get("sim.hash_bits")) for s in common]))


def nearest_past(current: Mapping[str, Any], past: Iterable[tuple[str, Mapping[str, Any]]]) -> tuple[str, float] | None:
    """``(duo id, distance)`` of the nearest past duo, comparing only vectors of the same mode."""
    best: tuple[str, float] | None = None
    for duo_id, vec in past:
        d = memory_distance(current, vec)
        if d is not None and (best is None or d < best[1]):
            best = (duo_id, d)
    return best


