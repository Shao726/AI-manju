# MiniMax H3 R2V 后端接口 README

> 版本：v1
> 状态：后端接口、ComfyUI workflow pack、任务处理器和自动化测试已实现
> 目标：通过参考图片保留角色、产品或主体的外观，在新的场景和动作中生成带同步音频的视频

## 1. 第一版范围

v1 实现单段 r2v：

```text
一个 Shot
  -> 1 到 9 张参考图
  -> 一段 prompt、一个时长、一个 seed
  -> MiniMaxH3ReferenceToVideo
  -> 一个带同步音频的 MP4 Take
```

r2v 的参考图不是首帧或尾帧。它们用于定义主体的身份、外观、服装和标志性细节。提示词通过 `<Picture N>` 引用图片。

v1 不包含：

- 参考视频 `<Video N>`
- 参考音频 `<Audio N>`
- 多段 Director 时间轴
- 分段重跑
- 前端选择器或上传界面
- 导演台 Refine、FaceRefine、放大

这些属于后续 v2。

## 2. 对外接口

### 2.1 地址约定

StoryBored 后端默认监听：

```text
http://127.0.0.1:8600
```

部署到远程服务器后，地址由部署方式决定：

```text
局域网：http://<backend-host>:8600
HTTPS：https://<api-domain>
反向代理子路径：https://<domain>/<prefix>
```

下文统一使用：

```text
BASE_URL=https://<api-domain>
```

如果没有反向代理、直接使用端口：

```text
BASE_URL=http://<backend-host>:8600
```

所有业务接口都拼接在 `BASE_URL` 后面，并以 `/api` 开头。

注意区分：

- 浏览器调用的是 `BASE_URL` 对应的 StoryBored 后端。
- 后端内部调用的是 `COMFYUI_URL` 对应的 ComfyUI。

### 2.2 完整接口地址

| 用途 | 方法 | 完整地址 |
| --- | --- | --- |
| 创建 R2V 渲染任务 | `POST` | `{BASE_URL}/api/shots/{shot_id}/render-r2v` |
| 查询任务 | `GET` | `{BASE_URL}/api/jobs/{job_id}` |
| 取消任务 | `POST` | `{BASE_URL}/api/jobs/{job_id}/cancel` |
| 订阅任务事件 | `GET` | `{BASE_URL}/api/events` |
| 获取生成视频 | `GET` | `{BASE_URL}/api/media/media/{project_id}/{shot_id}/take_{take_id}.mp4` |

例如后端直接部署在 `http://192.168.1.50:8600`：

```text
POST http://192.168.1.50:8600/api/shots/48/render-r2v
```

例如后端通过 Nginx 暴露为 `https://api.example.com`：

```text
POST https://api.example.com/api/shots/48/render-r2v
```

ComfyUI 地址是后端配置项，不是浏览器接口：

```env
COMFYUI_URL=http://127.0.0.1:8188
```

后端实际调用：

```text
{COMFYUI_URL}/upload/image
{COMFYUI_URL}/prompt
{COMFYUI_URL}/history/{prompt_id}
{COMFYUI_URL}/view
{COMFYUI_URL}/object_info
```

### 2.3 创建 R2V 渲染任务

### `POST /api/shots/{shot_id}/render-r2v`

返回 `202 Accepted`。

请求格式为 `multipart/form-data`。

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `reference_images` | File[] | 是 | 1 到 9 张 PNG/JPEG/WebP，每张最大 20 MiB |
| `prompt` | string | 否 | 为空时使用 `shot.motion_prompt`，再回退到 `shot.description` |
| `duration_s` | float | 否 | 默认取 Shot 时长且不低于 5 秒；范围 0.2 到 20 秒 |
| `width` | int | 否 | 默认按项目宽高比推导；必须与 `height` 同时提供 |
| `height` | int | 否 | 必须与 `width` 同时提供；宽高必须是 32 的倍数 |
| `seed` | int | 否 | 0 到 `2^32 - 1`；未提供时随机生成并持久化 |
| `ref_image_size` | string | 否 | `match` 或 `max`，默认 `match` |

图片顺序决定 `<Picture N>`：

```text
reference_images[0] -> <Picture 1>
reference_images[1] -> <Picture 2>
reference_images[2] -> <Picture 3>
```

### 请求示例

```bash
BASE_URL="http://192.168.1.50:8600"

curl -X POST "$BASE_URL/api/shots/48/render-r2v" \
  -F "reference_images=@hero.png" \
  -F "reference_images=@costume.png" \
  -F "prompt=<Picture 1> wears the outfit from <Picture 2> and walks through a rain-soaked alley. Audio: rain, footsteps, distant traffic." \
  -F "duration_s=5" \
  -F "width=864" \
  -F "height=480" \
  -F "seed=42" \
  -F "ref_image_size=match"
```

### 响应示例

```json
{
  "job_id": 1234,
  "status": "queued",
  "workflow_id": "minimax-h3-r2v",
  "reference_count": 2,
  "frame_count": 124,
  "duration_s": 5.0,
  "seed": 42
}
```

调用方随后使用：

```text
GET /api/jobs/{job_id}
POST /api/jobs/{job_id}/cancel
GET /api/events
```

## 3. 错误码

| 情况 | HTTP |
| --- | --- |
| Shot 不存在 | `404` |
| 没有参考图 | `400` |
| 超过 9 张参考图 | `400` |
| 图片不是有效图片 | `400` |
| 图片不是 PNG/JPEG/WebP | `400` |
| 单张图片超过 20 MiB | `413` |
| `width` 或 `height` 只提供一个 | `400` |
| 宽高不是 32 的倍数 | `400` |
| duration 或 seed 超范围 | `400` |
| 工作流缺模型或缺节点 | `409` |
| ComfyUI 不可达 | `503` |

错误响应格式沿用 FastAPI：

```json
{
  "detail": "human-readable error"
}
```

## 4. 工作流

新增 workflow pack：

```text
workflows/minimax-h3-r2v/
  manifest.json
  graph.json
```

`graph.json` 是 ComfyUI API 格式，不是编辑器格式。

关键节点：

| 节点 | 类型 | 作用 |
| --- | --- | --- |
| `1` | `UNETLoader` | `minimax_h3_ref2va_pruned_int8_convrot.safetensors` |
| `2` | `CLIPLoader` | `qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors` |
| `3` | `VAELoader` | 视频 VAE |
| `4` | `VAELoader` | 音频 VAE |
| `5` | `MiniMaxH3ReferenceToVideo` | 参考条件、latent、prompt、尺寸、时长 |
| `6` | `BasicScheduler` | simple / 20 steps / denoise 1.0 |
| `7` | `KSamplerSelect` | `res_multistep` |
| `8` | `RandomNoise` | seed |
| `9` | `BasicGuider` | ref2va conditioning |
| `10` | `SamplerCustomAdvanced` | 采样 |
| `11` | `VAEDecode` | 视频解码 |
| `12` | `VAEDecodeAudio` | 音频解码 |
| `13` | `CreateVideo` | 音视频合成，24 fps |
| `14` | `SaveVideo` | 输出节点 |
| `20+` | `LoadImage` | 动态参考图输入 |

后端会为每张参考图添加一个 `LoadImage` 节点，并动态写入：

```json
{
  "5": {
    "class_type": "MiniMaxH3ReferenceToVideo",
    "inputs": {
      "ref_images.ref_image_0": ["20", 0],
      "ref_images.ref_image_1": ["21", 0],
      "ref_images.ref_image_2": ["22", 0]
    }
  }
}
```

`ref_images.ref_image_N` 是 ComfyUI Autogrow 在 API 格式中的扁平点号输入名。

## 5. 模型要求

必须安装：

| 用途 | 文件 |
| --- | --- |
| r2v UNET | `minimax_h3_ref2va_pruned_int8_convrot.safetensors` |
| CLIP | `qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors` |
| Video VAE | `minimax_h3_video_vae_fp16.safetensors` |
| Audio VAE | `minimax_h3_audio_vae_fp32.safetensors` |

r2v 不能使用 `minimax_h3_fl2va_*` 模型。fl2va 用于 t2v/i2v/fl2v，r2v 必须使用 ref2va。

工作流还需要较新的 ComfyUI，至少包含：

```text
MiniMaxH3ReferenceToVideo
VAEDecodeAudio
CreateVideo
SaveVideo
```

## 6. 后端执行流程

1. 校验 Shot、Scene、Project 归属关系。
2. 校验 1 到 9 张参考图。
3. 用 Pillow 验证图片内容和格式，读取宽高。
4. 预检 `minimax-h3-r2v` 工作流、模型和节点。
5. 将图片保存到：

```text
DATA_DIR/media/{project_id}/{shot_id}/r2v_refs/{batch_id}/ref_00.png
```

6. 创建 `r2v_gen` GPU 任务。
7. 任务处理器再把图片上传到 ComfyUI `/upload/image`。
8. 构造 API graph，用 `LoadImage` 节点连接 `MiniMaxH3ReferenceToVideo.ref_images.*`。
9. 应用 `engine_models` 和 `engine_loras` 设置。
10. 设置 `SaveVideo.filename_prefix = storybored/take_{take_id}`。
11. 提交 ComfyUI `/prompt` 并轮询 `/history/{prompt_id}`。
12. 从节点 `14` 下载 MP4。
13. 生成首帧缩略图并更新 Take 和 Shot。
14. 通过 SSE 发布 `job`、`take`、`shot`。

## 7. 帧数

接口接受秒数，但 ComfyUI 使用帧数。后端自动对齐到 MiniMax 的 `17k+5` 网格：

```python
def minimax_frame_count(duration_s: float, fps: float = 24.0) -> int:
    n = max(5, int(round(max(0.1, float(duration_s)) * fps)))
    return n + ((5 - (n % 17)) % 17)
```

示例：

```text
5 秒  -> 124 帧
10 秒 -> 243 帧
15 秒 -> 372 帧
```

## 8. 输出

成功后 Take 示例：

```json
{
  "id": 91,
  "shot_id": 48,
  "kind": "video",
  "status": "done",
  "file_path": "media/1/48/take_91.mp4",
  "thumb_path": "media/1/48/take_91_thumb.png",
  "workflow_id": "minimax-h3-r2v",
  "seed": 42
}
```

`params_json` 会保存：

```json
{
  "prompt": "...",
  "duration_s": 5,
  "length": 124,
  "width": 864,
  "height": 480,
  "seed": 42,
  "ref_image_size": "match",
  "reference_files": [
    {
      "path": "media/1/48/r2v_refs/abc123/ref_00.png",
      "file_name": "hero.png",
      "width": 96,
      "height": 64
    }
  ],
  "comfy_references": [
    {
      "image": "storybored/1/48/storybored_take_91_ref_0.png",
      "local_path": "media/1/48/r2v_refs/abc123/ref_00.png"
    }
  ]
}
```

结果 JSON：

```json
{
  "take_id": 91,
  "file_path": "media/1/48/take_91.mp4",
  "reference_count": 1
}
```

视频通过以下地址访问：

```text
GET /api/media/media/1/48/take_91.mp4
```

## 9. 前端接入建议

页面需要提供：

- 参考图多选上传区，最多 9 张；
- 图片排序和删除；
- `<Picture N>` 与缩略图的对应关系；
- prompt 输入框；
- duration、width、height、seed；
- `match` / `max` 参考图尺寸策略；
- 提交后显示 job 进度；
- 完成后显示视频和缩略图。

如果前端已经使用 Shot Drawer，可以把 r2v 作为 Shot 的另一种视频生成模式。它不要求 Shot 已 approve，也不要求已经有 picked still。

## 10. 与 i2v、fl2v 的边界

| 模式 | 输入语义 |
| --- | --- |
| i2v | 图片作为视频首帧 |
| fl2v | 首帧和/或尾帧作为时间轴关键帧 |
| r2v | 图片作为主体身份、外观和产品参考 |

r2v 不应该把参考图强制当作视频第一帧。提示词应描述新场景和动作，并用 `<Picture N>` 指定参考图。

## 11. 代码位置

```text
backend/storybored/api/r2v.py
backend/storybored/engine/r2v.py
backend/tests/test_r2v.py
frontend/src/lib/types.ts
frontend/src/lib/format.ts
workflows/minimax-h3-r2v/graph.json
workflows/minimax-h3-r2v/manifest.json
```

相关接入点：

```text
backend/storybored/main.py
backend/storybored/jobs/runner.py
backend/storybored/api/workflows_api.py
```

## 12. 验证结果

已通过：

```text
python -m storybored validate-pack ../workflows/minimax-h3-r2v
python -m pytest tests/test_r2v.py -q
ruff check storybored/api/r2v.py storybored/engine/r2v.py tests/test_r2v.py
```

r2v 测试：`5 passed`

与视频、工作流导入/导出、pack 校验相关的测试：

```text
53 passed
```

## 13. v2 建议

v2 再考虑：

1. 参考视频 `<Video N>` 和其对应音轨。
2. 独立参考音频 `<Audio N>`。
3. 多段 Director 时间轴。
4. 每段不同参考素材和时长。
5. 参考图持久化到 Shot/Character 级资源库。
6. 分段重跑和成本统计。
