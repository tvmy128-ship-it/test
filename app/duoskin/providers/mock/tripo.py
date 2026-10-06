"""MockTripo: a deterministic Tripo v3 state machine (APP_SPEC 7.8).

It shares the real adapter's request builders and validation (``TripoCommon``: the same bodies, the same ``check_body``,
the same named-view rules, ``wait``, ``reconcile_uncertain``), so a pipeline bug that the real adapter would reject is
rejected here too. Tasks go ``queued`` (first poll) -> ``running`` (two polls) -> ``success``.

* ``image_to_multiview`` / ``edit_multiview`` return four views rendered from a procedural trimesh primitive whose colour is the
  main colour of the uploaded image; ``multiview_to_model`` / ``image_to_model`` / ``text_to_model`` return a textured GLB
  (icosphere, rounded box, one-sided prop or plush; about 2k triangles; one material; a 1024 PNG) and a rendered preview,
  with ``credits_consumed``. A hair request (``face_limit`` 3500, a ``hair`` step tag or a ``hair`` upload name) returns a
  hair-like shell WITH the grey cube head (``#9A9A9A``) still attached, so ``hair.register`` runs for real.
* ``convert`` returns GLB (``GLTF``), a ZIP (``OBJ``) or magic-bytes-only FBX (the mock cannot write a real FBX).
* ``balance`` returns ``(2000, 0)``; with ``track_balance=True`` credits are frozen while a task runs and deducted at success.
* Faults: ``1007``/``rate_limit``, ``2000``/``concurrency``, ``2010``, ``2015``, ``auth``, ``connect_error`` raise at submit with the
  real errors; ``2008``/``moderation``, ``2018``/``queue_expired`` and any other error code make the task FAIL with that
  ``error_code``; ``slow`` keeps the task running for 30 polls; ``read_timeout_after_send`` creates the task and raises
  ``submission_uncertain`` (so ``reconcile_uncertain`` finds it).
"""
from __future__ import annotations

import hashlib
import io
import random
import threading
import uuid
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import trimesh
from PIL import Image

from duoskin.providers.base import CallCtx, CapabilityFlags, ProviderError, request_hash, seed_from_hash, sniff_kind
from duoskin.providers.faults import FaultInjector
from duoskin.providers.mock import _draw as D
from duoskin.providers.mock import meshes as M
from duoskin.providers.mock._common import MockBase
from duoskin.providers.pricing import tripo_cost, tripo_credits
from duoskin.providers.tripo import (
    ENDPOINTS,
    FACE_LIMIT,
    P2,
    DownloadedFile,
    RemoteStatus,
    TripoCommon,
    check_body,
    check_upload,
    expected_kinds,
    parse_task,
    uncertain_error,
)

T_CODE = {"image_to_multiview": "T1", "edit_multiview": "T2", "multiview_to_model": "T3", "image_to_model": "T4", "convert": "T5",
          "import_model": "T5", "text_to_model": "T3", "texture_model": "T5", "mesh_segment": "T5", "mesh_complete": "T5", "retopology": "T5"}
MODEL_OPS = frozenset({"multiview_to_model", "image_to_model", "text_to_model"})
FAIL_CODES = {"2008": (2008, "The content was rejected by Tripo's safety filter."), "moderation": (2008, "The content was rejected by Tripo's safety filter."),
              "2018": (2018, "The task waited too long in the queue and expired."), "queue_expired": (2018, "The task waited too long in the queue and expired."),
              "task_failed": (5000, "The generation failed.")}
PRIMITIVES = ("icosphere", "rounded_box", "one_sided_prop", "plush_pet")


@dataclass
class _Task:
    id: str
    op: str
    body: dict[str, Any]
    model: str
    created_at: datetime
    credits: float
    kind: str = "icosphere"
    color: tuple[int, int, int] = (180, 90, 60)
    seed: int = 0
    mesh_seed: int = 0
    polls: int = 0
    running_polls: int = 2
    fail: tuple[int, str] | None = None
    blobs: dict[str, bytes] = field(default_factory=dict)
    done: bool = False
    frozen: float = 0.0
    source: str | None = None


class MockTripo(MockBase, TripoCommon):
    """Deterministic Tripo provider. ``requests`` holds every paid call; ``costs`` the mock cost rows."""

    name = "tripo"

    def __init__(self, *, faults: FaultInjector | None = None, cost_sink: Callable[[dict[str, Any]], None] | None = None,
                 flags: CapabilityFlags | None = None, starting_balance: float = 2000.0, track_balance: bool = False,
                 running_polls: int = 2, now: Callable[[], datetime] | None = None, view_size: int = 768,
                 sleep: Callable[[float], None] | None = None) -> None:
        MockBase.__init__(self, faults=faults, cost_sink=cost_sink, flags=flags)
        self._now = now or (lambda: datetime.now(UTC))
        self._t = 0.0
        self._sleep = sleep or self._advance
        self._clock = lambda: self._t
        self._ops: dict[str, tuple[str, str, int]] = {}
        self._costed: set[str] = set()
        self._tasks: dict[str, _Task] = {}
        self._files: dict[str, tuple[bytes, str]] = {}
        self._seq = 0
        self._tlock = threading.RLock()
        self.starting_balance = float(starting_balance)
        self.track_balance = track_balance
        self.running_polls = running_polls
        self.view_size = view_size
        self._spent = 0.0
        self._frozen = 0.0

    def __repr__(self) -> str:
        return "MockTripo()"

    def _advance(self, seconds: float) -> None:
        self._t += seconds

    # ----- account ------------------------------------------------------------------------------------------
    def balance(self) -> tuple[float, float]:
        with self._tlock:
            if not self.track_balance:
                return self.starting_balance, 0.0
            return self.starting_balance - self._spent, self._frozen

    def usage(self, *, limit: int = 50, offset: int = 0) -> list[dict]:
        with self._tlock:
            rows = sorted(self._tasks.values(), key=lambda t: (t.created_at, t.id), reverse=True)
            return [{"task_id": t.id, "type": t.op, "status": self._status_of(t), "credits_consumed": t.credits if (t.done and not t.fail) else 0.0,
                     "created_at": t.created_at.isoformat()} for t in rows][offset:offset + limit]

    @staticmethod
    def _status_of(t: _Task) -> str:
        return ("failed" if t.fail else "success") if t.done else "running"

    # ----- uploads ------------------------------------------------------------------------------------------
    def upload(self, png: bytes, *, name: str, ctx: CallCtx | None = None) -> str:
        check_upload(png)
        token = "mock-file-" + hashlib.sha256(png).hexdigest()[:16]
        with self._tlock:
            self._files[token] = (bytes(png), name)
        return token

    # ----- task creation ------------------------------------------------------------------------------------
    def _image_of(self, token: str) -> tuple[bytes, str]:
        if token not in self._files:
            raise ProviderError("tripo", "bad_request", "unknown or expired file token", http=400, code="unknown_file_token", billed="no",
                                user_hint="The Tripo upload token is unknown. Upload the image again just before the request.")
        return self._files[token]

    def _task_of(self, task_id: str, *, need_success: bool = False) -> _Task:
        t = self._tasks.get(task_id)
        if t is None:
            raise ProviderError("tripo", "bad_request", "unknown task id", http=400, code="unknown_task", billed="no")
        return t

    def _create(self, op: str, body: dict[str, Any], ctx: CallCtx | None, *, model: str = P2, views: int = 1,
                route: str | None = None, allow: Iterable[str] = ()) -> str:
        ctx = ctx or CallCtx.null()
        if route is not None:
            check_body(body, route, allow)
        with self._tlock:
            digest = request_hash({"op": op, "body": body, "seq": self._seq})
            submitted_at = self._now()
            credits = tripo_credits(op, views=views, route=route)
            t = _Task(id=str(uuid.uuid5(uuid.NAMESPACE_URL, f"mock-tripo:{digest}")), op=op, body=dict(body), model=model, created_at=submitted_at,
                      credits=credits, running_polls=self.running_polls)
            self._prepare(t, body, ctx, route)             # unknown tokens or tasks are rejected (HTTP 400) before anything counts as sent
            self.record("tripo", op, {"op": op, "body": body})
            ctx.tick()
            tcode = T_CODE.get(op, "T3")
            behaviour = self.fault("tripo", tag=ctx.tag or tcode, seed=body.get("model_seed"), aliases=[tcode, op, ctx.tag])
            self._seq += 1
            if behaviour in FAIL_CODES:
                t.fail = FAIL_CODES[behaviour]
            elif behaviour and behaviour.isdigit():
                t.fail = (int(behaviour), "The task failed.")
            elif behaviour == "slow":
                t.running_polls = 30
            self._tasks[t.id] = t
            self._ops[t.id] = (op, model, views)
            if self.track_balance:
                t.frozen = credits
                self._frozen += credits
            if behaviour == "read_timeout_after_send":     # the server received it, the answer was lost
                raise uncertain_error(op, ENDPOINTS[op], body, submitted_at, "ReadTimeout after the request was sent")
            ctx.set_remote_ref(t.id)
            return t.id

    def _prepare(self, t: _Task, body: dict[str, Any], ctx: CallCtx, route: str | None) -> None:
        """Pick the mesh kind and colour of a task from its inputs (deterministic)."""
        op = t.op
        t.seed = int(body.get("model_seed", 0) or 0)
        hair = body.get("face_limit") == FACE_LIMIT["hair_custom"] or "hair" in (ctx.tag or "").lower()
        src_img: bytes | None = None
        if op == "image_to_multiview" or op == "image_to_model":
            src_img, name = self._image_of(body["input"])
            hair = hair or "hair" in name.lower()
        elif op == "multiview_to_model":
            inputs = body["inputs"]
            if len(inputs) == 1 and "task_id" in inputs[0]:
                base = self._task_of(inputs[0]["task_id"])
                t.kind, t.color, t.source, t.mesh_seed = base.kind, base.color, base.id, base.mesh_seed
                hair = hair or base.kind == "hair_with_head"
            else:
                toks = {k: v for d in inputs for k, v in d.items()}
                for tok in toks.values():
                    self._image_of(tok)
                src_img, name = self._image_of(toks["front"])
                hair = hair or any("hair" in self._files[tok][1].lower() for tok in toks.values())
        elif op == "edit_multiview":
            base = self._task_of(body["input"])
            t.kind, t.color, t.source, t.mesh_seed = base.kind, base.color, base.id, base.mesh_seed
        elif op == "text_to_model":
            h = request_hash({"prompt": body["prompt"]})
            t.kind = M.KINDS[int(h[:4], 16) % len(PRIMITIVES)] if not hair else "hair_with_head"
            t.color = (D.colors_from_text(body["prompt"]) or [D.fallback_colors(random.Random(h[:8]), 1)[0]])[0]
        elif op in ("convert", "texture_model", "mesh_segment", "retopology", "mesh_complete"):
            src = body.get("input")
            base = self._tasks.get(src)
            if base is None and src in self._files:       # a raw file token
                data, _ = self._files[src]
                t.kind, t.color = ("hair_with_head" if hair else PRIMITIVES[int(hashlib.sha256(data).hexdigest()[:4], 16) % 4]), (150, 150, 160)
            elif base is None:
                raise ProviderError("tripo", "bad_request", "unknown source task or file token", http=400, code="unknown_task", billed="no")
            else:
                t.kind, t.color, t.source, t.mesh_seed = base.kind, base.color, base.id, base.mesh_seed
        elif op == "import_model":
            self._image_of(body["input"])
        if src_img is not None:
            h = hashlib.sha256(src_img).hexdigest()
            t.kind = "hair_with_head" if hair else PRIMITIVES[int(h[:4], 16) % len(PRIMITIVES)]
            t.color = D.mean_color(src_img) if sniff_kind(src_img) in ("png", "jpeg") else (150, 150, 160)
            t.mesh_seed = int(h[4:8], 16)
        if hair and op in MODEL_OPS:
            t.kind = "hair_with_head"
        if op in MODEL_OPS and t.source is None:
            t.mesh_seed = (t.mesh_seed + t.seed) % 65536                    # a new model seed gives a new variant of the same design

    # ----- state machine ------------------------------------------------------------------------------------
    def task(self, task_id: str, *, ctx: CallCtx | None = None) -> RemoteStatus:
        with self._tlock:
            t = self._tasks.get(task_id)
            if t is None:
                raise ProviderError("tripo", "not_found", "no such task", http=404, code="1005", billed="no")
            t.polls += 1
            data: dict[str, Any] = {"task_id": t.id, "type": t.op, "input": self._input_of(t), "output": {}}
            if t.polls == 1:
                data.update(status="queued", progress=0)
            elif t.polls <= 1 + t.running_polls:
                data.update(status="running", progress=min(95, 30 * (t.polls - 1)))
            else:
                self._finish_task(t)
                if t.fail:
                    data.update(status="failed", progress=0, error_code=t.fail[0], error_message=t.fail[1], credits_consumed=0.0)
                else:
                    data.update(status="success", progress=100, credits_consumed=t.credits,
                                output={k: f"https://tripo-data.rg1.data.tripo3d.com/mock/{t.id}/{k}" for k in t.blobs})
            return parse_task(data)

    @staticmethod
    def _input_of(t: _Task) -> dict[str, Any]:
        return {k: v for k, v in t.body.items() if k not in ("inputs", "input", "prompts", "prompt", "negative_prompt")}

    def _finish_task(self, t: _Task) -> None:
        if t.done:
            return
        t.done = True
        if self.track_balance:
            self._frozen = max(0.0, self._frozen - t.frozen)
            if not t.fail:
                self._spent += t.credits
        if not t.fail:
            t.blobs = self._outputs(t)

    def _outputs(self, t: _Task) -> dict[str, bytes]:
        op = t.op
        if op in ("image_to_multiview", "edit_multiview"):
            mesh = M.build_mesh(t.kind, t.color, t.mesh_seed)
            views = M.render_views(mesh, self.view_size)
            if op == "edit_multiview":
                for item in t.body["prompts"]:
                    tint = (D.colors_from_text(item["prompt"]) or [(255, 200, 80)])[0]
                    views[item["view"]] = _tint_png(views[item["view"]], tint)
            return {f"{v}_view_url": png for v, png in views.items()}
        if op == "import_model":
            data, _ = self._files[t.body["input"]]
            return {"model_url": data}
        mesh = M.build_mesh(t.kind, t.color, t.mesh_seed)
        glb = bytes(mesh.export(file_type="glb"))
        out: dict[str, bytes] = {}
        if op == "convert":
            fmt = t.body.get("format", "GLTF")
            out["model_url"] = M.to_obj_zip(mesh) if fmt == "OBJ" else M.fake_fbx(mesh) if fmt == "FBX" else glb
            return out
        out["model_url"] = glb
        if op in MODEL_OPS:
            out["rendered_image_url"] = M.render_views(mesh, 512, views=("front",))["front"]
        return out

    # ----- downloads ----------------------------------------------------------------------------------------
    def download_files(self, task_id: str, keys: list[str], *, ctx: CallCtx | None = None, max_reget: int = 3,
                       status: RemoteStatus | None = None) -> dict[str, DownloadedFile]:
        st = status or self.task(task_id, ctx=ctx)
        if st.status != "success":
            raise ProviderError("tripo", "bad_request", f"task {task_id} is {st.status}, not success", code="not_success", billed="no")
        t = self._tasks[task_id]
        out: dict[str, DownloadedFile] = {}
        for key in keys:
            data = t.blobs.get(key)
            if data is None:
                raise ProviderError("tripo", "validation", f"the task has no {key}", code="missing_output", billed="yes")
            kind = sniff_kind(data)
            if kind not in expected_kinds(key):
                raise ProviderError("tripo", "validation", f"{key} has unexpected content ({kind})", code=f"bad_magic_{kind}", billed="yes")
            out[key] = DownloadedFile(key=key, data=data, kind=kind, sha256=hashlib.sha256(data).hexdigest(), size=len(data))
        return out

    def download(self, task_id: str, keys: list[str], *, ctx: CallCtx | None = None) -> dict[str, bytes]:
        return {k: f.data for k, f in self.download_files(task_id, keys, ctx=ctx).items()}

    def tasks(self, ids: list[str]) -> tuple[dict[str, RemoteStatus], list[str]]:
        found: dict[str, RemoteStatus] = {}
        missed: list[str] = []
        for tid in ids:
            if tid in self._tasks:
                found[tid] = self.task(tid)
            else:
                missed.append(tid)
        return found, missed

    # ----- cost (the mock reports the same credits the real task would) ----------------------------------------
    def _finish(self, st: RemoteStatus) -> None:
        if st.task_id in self._costed:
            return
        credits = st.credits_consumed
        if st.status != "success" and not credits:
            return
        op, model, views = self._ops.get(st.task_id, (st.type or "task", str(st.input.get("model", "")), 1))
        self._costed.add(st.task_id)
        self.record_cost(tripo_cost(str(st.input.get("model") or model), operation=op, credits=credits, task_id=st.task_id,
                                    fallback_op=op if op in ENDPOINTS else None, views=views))


def _tint_png(png: bytes, tint: tuple[int, int, int]) -> bytes:
    """Blend a view toward ``tint`` wherever it is not the white backdrop (what an edited view looks like)."""
    import numpy as np
    arr = np.asarray(Image.open(io.BytesIO(png)).convert("RGB")).astype(np.float32)
    obj = (np.abs(arr - 255).sum(axis=-1) > 12)[..., None]
    arr = np.where(obj, 0.6 * arr + 0.4 * np.array(tint, np.float32), arr)
    return D.to_png(Image.fromarray(arr.astype(np.uint8), "RGB"))


def seed_of(data: bytes) -> int:  # pragma: no cover - small helper kept for callers that want a stable seed from bytes
    return seed_from_hash(hashlib.sha256(data).hexdigest())


_ = trimesh  # trimesh is imported so a missing install fails at import time, not at the first model request
