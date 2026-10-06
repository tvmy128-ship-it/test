"""``POST /api/uploads/mask`` (APP_SPEC §13): the brush mask of a local edit, sized to the tile image."""
from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, File, HTTPException, UploadFile

from duoskin.api import RT
from duoskin.engine.cas import CasError, make_prov
from duoskin.engine.runtime import Runtime
from duoskin.security import MAX_IMAGE_PIXELS

router = APIRouter(prefix="/api")
MAX_MASK_BYTES = 4 * 1024 * 1024


@router.post("/uploads/mask")
def upload_mask(file: Annotated[UploadFile, File()], rt: Runtime = RT) -> dict[str, str]:
    """A multipart PNG, RGBA, with alpha 0 where the model may change the image and 255 elsewhere (the OpenAI mask convention). Returns ``{sha}``."""
    from duoskin.imaging import masks

    data = file.file.read(MAX_MASK_BYTES + 1)
    if len(data) > MAX_MASK_BYTES:
        raise HTTPException(status_code=413, detail={"error": "too_large", "message": "the mask must be smaller than 4 MB"})
    try:
        import io

        from PIL import Image

        with Image.open(io.BytesIO(data)) as im:
            size = im.size
    except Exception as exc:
        raise HTTPException(status_code=415, detail={"error": "bad_mask", "message": "the mask is not a readable PNG"}) from exc
    if size[0] * size[1] > MAX_IMAGE_PIXELS:       # a 4 MB PNG can claim billions of pixels: refuse on the header, decode nothing
        raise HTTPException(status_code=413, detail={"error": "too_large", "message": "the mask has too many pixels"})
    problems = masks.validate_mask(data, size)
    if problems:
        raise HTTPException(status_code=422, detail={"error": "bad_mask", "message": "; ".join(problems)})
    try:
        asset = rt.cas.put(data, "png", prov=make_prov("user", stream="pipeline", notes=["brush_mask"]))
    except CasError as exc:
        raise HTTPException(status_code=415, detail={"error": "bad_mask", "message": str(exc)}) from exc
    return {"sha": asset.sha256}
