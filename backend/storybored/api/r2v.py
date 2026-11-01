"""POST /api/shots/{id}/render-r2v - reference-image to video v1.

This endpoint is intentionally narrower than the generic shot video endpoint:
MiniMax H3 r2v conditions on one to nine reference images rather than a picked
first frame. The API validates and stores those images, then queues a dedicated
``r2v_gen`` job.
"""

from __future__ import annotations

import random
import shutil
import uuid
from io import BytesIO
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from PIL import Image, UnidentifiedImageError
from sqlmodel import Session, select

from storybored.api.preflight import require_pack_available, resolve_pack
from storybored.api.shots import get_shot_or_404
from storybored.db import get_session
from storybored.engine import registry
from storybored.engine.graph import parse_mentions, substitute_mentions
from storybored.models import Character, Project, Scene

router = APIRouter(prefix="/api", tags=["r2v"])

R2V_WORKFLOW_ID = "minimax-h3-r2v"
MAX_REFERENCE_IMAGES = 9
MAX_REFERENCE_BYTES = 20 * 1024 * 1024
MIN_DURATION_S = 0.2
MAX_DURATION_S = 20.0
MAX_SEED = 2**32 - 1

_FORMAT_EXT = {"PNG": ".png", "JPEG": ".jpg", "WEBP": ".webp"}
_ASPECT_SIZES = {
    "1:1": (640, 640),
    "3:4": (576, 768),
    "4:3": (768, 576),
    "9:16": (480, 864),
    "16:9": (864, 480),
}


def minimax_frame_count(duration_s: float, fps: float = 24.0) -> int:
    """Snap seconds to MiniMax H3's 17k+5 frame grid."""
    n = max(5, int(round(max(0.1, float(duration_s)) * fps)))
    return n + ((5 - (n % 17)) % 17)


def _output_size(project: Project, width: int | None, height: int | None) -> tuple[int, int]:
    if (width is None) != (height is None):
        raise HTTPException(status_code=400, detail="width and height must be provided together")
    if width is None or height is None:
        width, height = _ASPECT_SIZES.get(project.aspect_ratio, (864, 480))
    if width < 32 or height < 32 or width > 8192 or height > 8192:
        raise HTTPException(status_code=400, detail="width and height must be between 32 and 8192")
    if width % 32 or height % 32:
        raise HTTPException(status_code=400, detail="width and height must be multiples of 32")
    return int(width), int(height)


def _decode_reference(data: bytes) -> tuple[str, int, int]:
    if not data:
        raise HTTPException(status_code=400, detail="reference image is empty")
    if len(data) > MAX_REFERENCE_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"each reference image must be at most {MAX_REFERENCE_BYTES} bytes",
        )
    try:
        with Image.open(BytesIO(data)) as image:
            fmt = str(image.format or "").upper()
            width, height = image.size
            image.verify()
    except (
        Image.DecompressionBombError,
        UnidentifiedImageError,
        OSError,
        ValueError,
    ) as exc:
        raise HTTPException(status_code=400, detail="reference image is not a valid image") from exc
    ext = _FORMAT_EXT.get(fmt)
    if ext is None:
        raise HTTPException(status_code=400, detail="reference images must be PNG, JPEG, or WebP")
    if width < 1 or height < 1:
        raise HTTPException(status_code=400, detail="reference image has invalid dimensions")
    return ext, width, height


async def _store_references(
    uploads: list[UploadFile], settings, project_id: int, shot_id: int
) -> tuple[list[dict], Path]:
    batch_id = uuid.uuid4().hex[:12]
    dest_dir = settings.media_path / str(project_id) / str(shot_id) / "r2v_refs" / batch_id
    dest_dir.mkdir(parents=True, exist_ok=False)
    stored: list[dict] = []
    try:
        for index, upload in enumerate(uploads):
            data = await upload.read(MAX_REFERENCE_BYTES + 1)
            ext, width, height = _decode_reference(data)
            name = f"ref_{index:02d}{ext}"
            path = dest_dir / name
            path.write_bytes(data)
            stored.append(
                {
                    "path": path.relative_to(settings.data_path).as_posix(),
                    "file_name": upload.filename or name,
                    "width": width,
                    "height": height,
                }
            )
    except Exception:
        shutil.rmtree(dest_dir, ignore_errors=True)
        raise
    return stored, dest_dir


def _prompt_for_shot(session: Session, shot, explicit_prompt: str) -> str:
    source = (explicit_prompt or "").strip() or shot.motion_prompt or shot.description or ""
    handles = parse_mentions(source)
    rows = (
        session.exec(select(Character).where(Character.handle.in_(handles))).all()
        if handles
        else []
    )
    return substitute_mentions(source, {c.handle: c for c in rows})


@router.post("/shots/{shot_id}/render-r2v", status_code=202)
async def render_r2v(
    shot_id: int,
    request: Request,
    reference_images: list[UploadFile] | None = File(default=None),
    prompt: str = Form(""),
    duration_s: float | None = Form(default=None),
    width: int | None = Form(default=None),
    height: int | None = Form(default=None),
    seed: int | None = Form(default=None),
    ref_image_size: Literal["match", "max"] = Form(default="match"),
    session: Session = Depends(get_session),
):
    settings = request.app.state.settings
    shot = get_shot_or_404(session, shot_id)
    scene = session.get(Scene, shot.scene_id)
    project = session.get(Project, scene.project_id) if scene is not None else None
    if scene is None or project is None:
        raise HTTPException(status_code=409, detail="shot has no parent project")

    uploads = [u for u in (reference_images or []) if (u.filename or "").strip()]
    if not uploads:
        raise HTTPException(status_code=400, detail="add at least one reference image")
    if len(uploads) > MAX_REFERENCE_IMAGES:
        raise HTTPException(
            status_code=400,
            detail=f"at most {MAX_REFERENCE_IMAGES} reference images are supported",
        )

    duration = float(duration_s if duration_s is not None else max(5.0, shot.duration_s))
    if not MIN_DURATION_S <= duration <= MAX_DURATION_S:
        raise HTTPException(
            status_code=400,
            detail=f"duration_s must be between {MIN_DURATION_S} and {MAX_DURATION_S}",
        )
    out_width, out_height = _output_size(project, width, height)
    if seed is not None and not 0 <= seed <= MAX_SEED:
        raise HTTPException(status_code=400, detail=f"seed must be between 0 and {MAX_SEED}")
    actual_seed = int(seed) if seed is not None else random.randrange(MAX_SEED + 1)
    prompt_text = _prompt_for_shot(session, shot, prompt)

    packs = registry.load_packs(settings)
    pack = resolve_pack(session, settings, packs, R2V_WORKFLOW_ID, kind="video")
    await require_pack_available(
        session, settings, pack, f"cannot validate workflow '{R2V_WORKFLOW_ID}'"
    )

    stored, dest_dir = await _store_references(
        uploads, settings, int(project.id), int(shot.id)
    )
    payload = {
        "shot_id": shot_id,
        "workflow_id": pack.id,
        "prompt": prompt_text,
        "duration_s": duration,
        "length": minimax_frame_count(duration),
        "width": out_width,
        "height": out_height,
        "seed": actual_seed,
        "ref_image_size": ref_image_size,
        "reference_files": stored,
    }
    try:
        job = request.app.state.runner.enqueue(
            "r2v_gen", payload, project_id=int(project.id)
        )
    except Exception:
        shutil.rmtree(dest_dir, ignore_errors=True)
        raise
    return {
        "job_id": job.id,
        "status": "queued",
        "workflow_id": pack.id,
        "reference_count": len(stored),
        "frame_count": payload["length"],
        "duration_s": duration,
        "seed": actual_seed,
    }
