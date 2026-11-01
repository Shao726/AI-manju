"""r2v_gen job handler: reference images -> MiniMax H3 ref2va video take."""

from __future__ import annotations

import asyncio
import json

from fastapi.encoders import jsonable_encoder
from sqlmodel import select

from storybored.engine import registry
from storybored.engine.comfy_client import ComfyCancelled, ComfyClient, ComfyError
from storybored.engine.graph import (
    added_engine_loras,
    apply_engine_lora_overrides,
    apply_model_overrides,
    apply_parameters,
    inject_style_loras,
    lora_injection_spec,
    parse_engine_loras,
    parse_engine_models,
    parse_mentions,
    set_filename_prefix,
    substitute_mentions,
)
from storybored.engine.video import (
    VIDEO_TIMEOUT_S,
    _pick_video_output,
    _update_take,
    _uploaded_name,
    make_video_thumbnail,
)
from storybored.jobs.registry import register
from storybored.jobs.runner import JobCancelled
from storybored.models import Character, Project, Scene, Shot, Take
from storybored.settings_store import effective_setting

R2V_WORKFLOW_ID = "minimax-h3-r2v"
MAX_REFERENCE_IMAGES = 9


def _make_client(comfy_url: str) -> ComfyClient:
    """Client factory seam used by tests."""
    return ComfyClient(comfy_url)


def _reference_nodes(
    graph: dict,
    uploaded: list[dict],
) -> None:
    director = graph.get("5")
    if director is None or director.get("class_type") != "MiniMaxH3ReferenceToVideo":
        raise RuntimeError("r2v workflow is missing MiniMaxH3ReferenceToVideo at node 5")
    inputs = director.setdefault("inputs", {})
    for key in [k for k in inputs if k.startswith("ref_images.")]:
        inputs.pop(key, None)
    if not uploaded:
        raise RuntimeError("r2v job has no reference images")
    if len(uploaded) > MAX_REFERENCE_IMAGES:
        raise RuntimeError(f"too many reference images (max {MAX_REFERENCE_IMAGES})")
    for index, ref in enumerate(uploaded):
        node_id = str(20 + index)
        graph[node_id] = {
            "class_type": "LoadImage",
            "inputs": {"image": ref["image"]},
        }
        inputs[f"ref_images.ref_image_{index}"] = [node_id, 0]


@register("r2v_gen")
async def r2v_gen(job, ctx):
    payload = json.loads(job.payload_json or "{}")
    shot_id = payload.get("shot_id")
    workflow_id = str(payload.get("workflow_id") or R2V_WORKFLOW_ID)
    settings = ctx.settings

    with ctx.session_factory() as session:
        shot = session.get(Shot, shot_id)
        if shot is None:
            raise RuntimeError(f"shot {shot_id} not found")
        scene = session.get(Scene, shot.scene_id)
        project = session.get(Project, scene.project_id) if scene is not None else None
        if scene is None or project is None:
            raise RuntimeError("shot has no parent project")
        project_id = int(project.id)
        prompt_source = str(payload.get("prompt") or shot.motion_prompt or shot.description or "")
        handles = parse_mentions(prompt_source)
        rows = (
            session.exec(select(Character).where(Character.handle.in_(handles))).all()
            if handles
            else []
        )
        by_handle = {c.handle: c for c in rows}
        comfy_url = effective_setting(session, settings, "comfyui_url")
        engine_loras = parse_engine_loras(
            effective_setting(session, settings, "engine_loras")
        ).get(workflow_id, [])
        engine_models = parse_engine_models(
            effective_setting(session, settings, "engine_models")
        ).get(workflow_id, {})

    references = payload.get("reference_files") or []
    if not references:
        raise RuntimeError("r2v job has no reference files")
    if len(references) > MAX_REFERENCE_IMAGES:
        raise RuntimeError(f"too many reference images (max {MAX_REFERENCE_IMAGES})")

    pack = registry.get_pack(settings, workflow_id)
    if pack is None:
        raise RuntimeError(f"unknown workflow '{workflow_id}'")
    if pack.manifest.get("kind") != "video":
        raise RuntimeError(f"workflow '{workflow_id}' is not a video workflow")

    seed = int(payload.get("seed") or 0)
    params = {
        "prompt": substitute_mentions(prompt_source, by_handle),
        "width": int(payload.get("width") or 864),
        "height": int(payload.get("height") or 480),
        "length": int(payload.get("length") or 124),
        "seed": seed,
        "ref_image_size": str(payload.get("ref_image_size") or "match"),
    }

    take = Take(
        shot_id=shot_id,
        kind="video",
        status="pending",
        workflow_id=pack.id,
        params_json=json.dumps(payload),
        seed=seed,
    )
    with ctx.session_factory() as session:
        session.add(take)
        session.commit()
        session.refresh(take)
    take_id = take.id
    ctx.publish("take", jsonable_encoder(take))

    client = _make_client(comfy_url)
    prompt_id: str | None = None
    try:
        ctx.raise_if_cancelled()
        ctx.update_progress(progress=0.05, detail="uploading reference images")
        uploaded: list[dict] = []
        subfolder = f"storybored/{project_id}/{shot_id}"
        for index, ref in enumerate(references):
            path = (settings.data_path / str(ref["path"])).resolve()
            if not path.is_relative_to(settings.data_path) or not path.is_file():
                raise RuntimeError(f"reference image {index} is missing on disk")
            suffix = path.suffix.lower() or ".png"
            result = await client.upload_image(
                path.read_bytes(),
                f"storybored_take_{take_id}_ref_{index}{suffix}",
                subfolder=subfolder,
                overwrite=False,
            )
            uploaded.append({"image": _uploaded_name(result), "local_path": ref["path"]})

        graph = apply_parameters(pack.load_graph(), pack.manifest, params)
        _reference_nodes(graph, uploaded)
        apply_model_overrides(graph, pack.manifest, engine_models)
        apply_engine_lora_overrides(graph, engine_loras)
        inject_style_loras(
            graph,
            lora_injection_spec(pack.manifest),
            added_engine_loras(engine_loras),
            id_prefix="engine_lora_",
        )
        set_filename_prefix(graph, f"storybored/take_{take_id}")

        ctx.raise_if_cancelled()
        ctx.update_progress(progress=0.1, detail="rendering reference video")
        prompt_id = await client.submit(graph)

        def on_status(pos):
            if pos is None or pos == 0:
                ctx.update_progress(detail="rendering reference video")
            else:
                ctx.update_progress(detail=f"engine queue position {pos}")

        entry = await asyncio.wait_for(
            client.wait_for(prompt_id, on_status=on_status, should_cancel=ctx.cancelled),
            timeout=VIDEO_TIMEOUT_S,
        )

        ctx.update_progress(progress=0.9, detail="downloading video")
        video_ref = _pick_video_output(entry, pack.manifest.get("output_node"))
        dest_dir = settings.media_path / str(project_id) / str(shot_id)
        dest = dest_dir / f"take_{take_id}.mp4"
        await client.download(
            video_ref.get("filename", ""),
            video_ref.get("subfolder", ""),
            video_ref.get("type", "output"),
            dest,
        )
        if not dest.is_file() or dest.stat().st_size == 0:
            raise ComfyError("downloaded video is empty")
        thumb = dest_dir / f"take_{take_id}_thumb.png"
        make_video_thumbnail(dest, thumb)
    except (JobCancelled, ComfyCancelled, asyncio.CancelledError) as exc:
        _update_take(ctx, take_id, status="failed", error="cancelled")
        if prompt_id is not None:
            try:
                await client.cancel(prompt_id)
            except BaseException:
                pass
        raise JobCancelled(str(exc)) from exc
    except TimeoutError as exc:
        _update_take(ctx, take_id, status="failed", error="video render timed out")
        if prompt_id is not None:
            try:
                await client.cancel(prompt_id)
            except BaseException:
                pass
        raise RuntimeError(f"video render timed out after {int(VIDEO_TIMEOUT_S)}s") from exc
    except Exception as exc:
        error = str(exc) if isinstance(exc, ComfyError) else f"{type(exc).__name__}: {exc}"
        _update_take(ctx, take_id, status="failed", error=error)
        raise

    final_params = {
        **payload,
        "comfy_references": uploaded,
    }
    _update_take(
        ctx,
        take_id,
        status="done",
        file_path=str(dest.relative_to(settings.data_path)),
        thumb_path=str(thumb.relative_to(settings.data_path)),
        params_json=json.dumps(final_params),
    )
    with ctx.session_factory() as session:
        current = session.get(Shot, shot_id)
        if current is not None:
            current.video_take_id = take_id
            session.add(current)
            session.commit()
            ctx.publish("shot", jsonable_encoder(current))
    ctx.update_progress(progress=1.0, detail="reference video ready")
    return {
        "take_id": take_id,
        "file_path": str(dest.relative_to(settings.data_path)),
        "reference_count": len(uploaded),
    }
