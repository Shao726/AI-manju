"""R2V v1 endpoint + handler tests, using a real tiny MP4 download fixture."""

import json
import shutil
import subprocess
import time
from io import BytesIO
from pathlib import Path

import imageio_ffmpeg
import pytest
from fake_comfy import fake_comfy, tiny_png  # noqa: F401 - pytest fixture
from PIL import Image
from sqlmodel import Session

import storybored.engine.r2v as r2v_mod
from storybored.engine.comfy_client import clear_object_info_cache
from storybored.models import Project, Scene, Shot

FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()


@pytest.fixture(autouse=True)
def engine(client, fake_comfy):  # noqa: F811 - fixture use
    r = client.put("/api/settings", json={"values": {"comfyui_url": fake_comfy.url}})
    assert r.status_code == 200, r.text
    return fake_comfy


def wait_for(client, job_id, statuses, timeout=60.0):
    deadline = time.monotonic() + timeout
    job = None
    while time.monotonic() < deadline:
        job = client.get(f"/api/jobs/{job_id}").json()
        if job["status"] in statuses:
            return job
        time.sleep(0.05)
    raise AssertionError(f"job {job_id} never reached {statuses}: {job}")


class FakeR2VClient:
    def __init__(self, result_mp4: Path):
        self.result_mp4 = result_mp4
        self.uploads: list[dict] = []
        self.graph: dict | None = None
        self.downloaded: tuple | None = None
        self.cancelled: list[str] = []

    async def upload_image(self, data, name, subfolder="", overwrite=True):
        self.uploads.append(
            {
                "bytes": data,
                "name": name,
                "subfolder": subfolder,
                "overwrite": overwrite,
            }
        )
        return {"name": name, "subfolder": subfolder, "type": "input"}

    async def submit(self, graph):
        self.graph = graph
        return "fake-r2v-prompt"

    async def wait_for(self, prompt_id, poll_interval=1.0, on_status=None, should_cancel=None):
        return {
            "status": {"completed": True, "status_str": "success"},
            "outputs": {
                "14": {
                    "videos": [
                        {"filename": "r2v.mp4", "subfolder": "storybored", "type": "output"}
                    ]
                }
            },
        }

    async def download(self, filename, subfolder="", folder_type="output", dest=None):
        self.downloaded = (filename, subfolder, folder_type)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(self.result_mp4, dest)
        return dest.read_bytes()

    async def cancel(self, prompt_id):
        self.cancelled.append(prompt_id)


@pytest.fixture
def fake_r2v_client(tmp_path, monkeypatch):
    mp4 = tmp_path / "fake_r2v_result.mp4"
    subprocess.run(
        [
            FFMPEG, "-y",
            "-f", "lavfi", "-i", "color=c=blue:s=320x240:r=24:d=0.25",
            "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
            str(mp4),
        ],
        check=True,
        capture_output=True,
    )  # fmt: skip
    fake = FakeR2VClient(mp4)
    monkeypatch.setattr(r2v_mod, "_make_client", lambda comfy_url: fake)
    return fake


def seed_shot(app, settings):
    with Session(app.state.engine, expire_on_commit=False) as session:
        project = Project(title="R2V Test", aspect_ratio="16:9")
        session.add(project)
        session.flush()
        scene = Scene(project_id=project.id, idx=0)
        session.add(scene)
        session.flush()
        shot = Shot(
            scene_id=scene.id,
            idx=0,
            description="A new scene using the supplied subject",
            duration_s=5.0,
        )
        session.add(shot)
        session.commit()
        return {"project_id": project.id, "shot_id": shot.id}


def reference_file(name: str = "ref.png", color=(20, 80, 160)):
    buf = BytesIO()
    Image.new("RGB", (96, 64), color).save(buf, format="PNG")
    return (name, buf.getvalue(), "image/png")


def test_r2v_happy_path(client, app, settings, fake_r2v_client):
    ids = seed_shot(app, settings)
    r = client.post(
        f"/api/shots/{ids['shot_id']}/render-r2v",
        data={
            "prompt": "Put <Picture 1> and <Picture 2> into a new shot",
            "duration_s": "5",
            "width": "480",
            "height": "864",
            "seed": "123",
            "ref_image_size": "match",
        },
        files=[
            ("reference_images", reference_file("hero.png")),
            ("reference_images", reference_file("prop.png", (160, 80, 20))),
        ],
    )
    assert r.status_code == 202, r.text
    body = r.json()
    assert body["reference_count"] == 2
    assert body["frame_count"] == 124

    job = wait_for(client, body["job_id"], {"done", "failed"})
    assert job["status"] == "done", job["error"]
    assert job["type"] == "r2v_gen"

    takes = client.get(f"/api/shots/{ids['shot_id']}/takes").json()
    take = next(t for t in takes if t["kind"] == "video")
    assert take["status"] == "done"
    assert take["workflow_id"] == "minimax-h3-r2v"
    assert (settings.data_path / take["file_path"]).is_file()
    assert (settings.data_path / take["thumb_path"]).is_file()

    assert len(fake_r2v_client.uploads) == 2
    assert all(u["overwrite"] is False for u in fake_r2v_client.uploads)
    graph = fake_r2v_client.graph
    assert graph is not None
    assert graph["5"]["inputs"]["prompt"].startswith("Put <Picture 1>")
    assert graph["5"]["inputs"]["width"] == 480
    assert graph["5"]["inputs"]["height"] == 864
    assert graph["5"]["inputs"]["length"] == 124
    assert graph["5"]["inputs"]["ref_images.ref_image_0"] == ["20", 0]
    assert graph["5"]["inputs"]["ref_images.ref_image_1"] == ["21", 0]
    assert graph["20"]["class_type"] == "LoadImage"
    assert graph["21"]["class_type"] == "LoadImage"
    assert graph["14"]["inputs"]["filename_prefix"] == f"storybored/take_{take['id']}"
    assert fake_r2v_client.downloaded == ("r2v.mp4", "storybored", "output")

    shot = client.get(f"/api/shots/{ids['shot_id']}").json()
    assert shot["video_take_id"] == take["id"]
    params = json.loads(take["params_json"])
    assert len(params["comfy_references"]) == 2
    assert json.loads(job["result_json"])["reference_count"] == 2


def test_r2v_rejects_invalid_or_too_many_images(client, app, settings):
    ids = seed_shot(app, settings)
    r = client.post(
        f"/api/shots/{ids['shot_id']}/render-r2v",
        data={"duration_s": "5"},
        files=[("reference_images", ("bad.png", b"not an image", "image/png"))],
    )
    assert r.status_code == 400
    assert "valid image" in r.json()["detail"]

    files = [
        ("reference_images", reference_file(f"ref_{i}.png", (i * 20, 20, 20)))
        for i in range(10)
    ]
    r = client.post(
        f"/api/shots/{ids['shot_id']}/render-r2v",
        data={"duration_s": "5"},
        files=files,
    )
    assert r.status_code == 400
    assert "at most 9" in r.json()["detail"]


def test_r2v_requires_reference_and_valid_size(client, app, settings):
    ids = seed_shot(app, settings)
    r = client.post(
        f"/api/shots/{ids['shot_id']}/render-r2v",
        data={"duration_s": "5"},
    )
    assert r.status_code == 400
    assert "at least one" in r.json()["detail"]

    r = client.post(
        f"/api/shots/{ids['shot_id']}/render-r2v",
        data={"duration_s": "5", "width": "481", "height": "864"},
        files=[("reference_images", reference_file())],
    )
    assert r.status_code == 400
    assert "multiples of 32" in r.json()["detail"]


def test_r2v_preflight_blocks_missing_ref2va(client, app, settings, engine, fake_r2v_client):
    ids = seed_shot(app, settings)
    engine.state.models["UNETLoader.unet_name"] = []
    clear_object_info_cache()
    r = client.post(
        f"/api/shots/{ids['shot_id']}/render-r2v",
        data={"duration_s": "5"},
        files=[("reference_images", reference_file())],
    )
    assert r.status_code == 409
    assert "minimax_h3_ref2va_pruned_int8_convrot.safetensors" in r.json()["detail"]
    assert client.get("/api/jobs").json() == []

def test_r2v_missing_shot_404(client, fake_r2v_client):
    r = client.post(
        "/api/shots/424242/render-r2v",
        data={"duration_s": "5"},
        files=[("reference_images", reference_file())],
    )
    assert r.status_code == 404
