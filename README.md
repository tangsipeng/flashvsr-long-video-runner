# flashvsr-long-video-runner

[中文说明](README.zh-CN.md)

An open-source-friendly wrapper around an **existing upstream FlashVSR checkout** for safer long-video upscaling.

This repository **does not vendor FlashVSR weights or modify the upstream project**. It adds:

- explicit chunk planning with frame-accurate manifests
- safer resume for long runs
- chunk-by-chunk rendering around the upstream `infer_flashvsr_v1.1_tiny_long_video.py`
- merged final MP4 output with original audio restored
- an optional async HTTP service for upload, queue, polling, cancellation, and result download

## At a glance

Use this repo in one of two ways:

1. CLI runner: generate a manifest, run it locally, and resume interrupted jobs.
2. Async service: let external systems upload videos, poll status, cancel queued/running jobs, and download results later.

Current queue model:

- one active render worker
- additional jobs stay in `queued`
- `--max-queued-jobs` can cap backlog size

## Quick Start

### Requirements

- Python 3.10+
- `ffmpeg` and `ffprobe` on `PATH`
- a working upstream FlashVSR environment with GPU dependencies installed

Expected upstream layout:

```text
/path/to/FlashVSR/
  examples/WanVSR/infer_flashvsr_v1.1_tiny_long_video.py
  examples/WanVSR/FlashVSR-v1.1/
```

### Install this wrapper

Run `plan`, `run`, and `serve` inside a Python environment that can already run upstream FlashVSR.

```bash
cd flashvsr-long-video-runner
pip install -e .
```

### Start and stop the service with one script

```bash
./scripts/flashvsr_service.sh start
./scripts/flashvsr_service.sh status
./scripts/flashvsr_service.sh logs
./scripts/flashvsr_service.sh stop
```

Start a 4x service:

```bash
./scripts/flashvsr_service.sh start 4
```

The script automatically:

- uses `PYTHONPATH=<repo>/src`
- prefers the upstream FlashVSR Python at `~/.openclaw/workspace/mycode/FlashVSR/.venv/bin/python`
- writes PID files under `.omx/state/`
- writes logs under `.omx/logs/`

You can override paths with environment variables such as `FLASHVSR_UPSTREAM_ROOT`, `FLASHVSR_PYTHON`, `FLASHVSR_PORT`, and `FLASHVSR_MAX_QUEUED_JOBS`.

### Start the async service

```bash
flashvsr-long-video serve \
  --host 0.0.0.0 \
  --port 8000 \
  --state-dir /data/flashvsr_service \
  --max-queued-jobs 8 \
  --upstream-root /path/to/FlashVSR
```

What the service does:

- accepts video uploads
- returns a `job_id` immediately when using progress-aware upload sessions
- renders jobs asynchronously in a single-worker queue
- stores uploads, manifests, chunks, and results under `--state-dir`
- resumes `queued` and `running` jobs after a service restart

### Submit a video

One-shot upload is still supported:

```bash
curl -sS -X POST "http://127.0.0.1:8000/v1/jobs?filename=input.mp4" \
  -H "Content-Type: application/octet-stream" \
  -H "X-Filename: input.mp4" \
  --data-binary @/data/input.mp4
```

The service returns `202 Accepted` with a `job_id`.

If you need server-side upload progress before the body finishes, create the job first and upload to its `upload` URL:

```bash
SIZE_BYTES="$(stat -c%s /data/input.mp4)"

curl -sS -X POST "http://127.0.0.1:8000/v1/jobs" \
  -H "Content-Type: application/json" \
  -d "{\"filename\":\"input.mp4\",\"size_bytes\":${SIZE_BYTES}}"
```

The response has `status: "uploading"`, `job_id`, and `urls.upload`. Send the bytes with:

```bash
curl -sS -X PUT "http://127.0.0.1:8000/v1/jobs/<job_id>/upload" \
  -H "Content-Type: application/octet-stream" \
  --data-binary @/data/input.mp4
```

### Poll progress

```bash
curl -sS "http://127.0.0.1:8000/v1/jobs/<job_id>"
```

Progress payload fields include:

- `status`: `uploading`, `queued`, `running`, `cancelling`, `cancelled`, `succeeded`, or `failed`
- `input.uploaded_bytes`
- `input.upload_percent`
- `progress.phase`: `uploading`, `queued`, `planning`, `rendering`, `finalizing`, `cancelling`, `cancelled`, `completed`, or `failed`
- `progress.percent`
- `progress.uploaded_bytes`
- `progress.total_upload_bytes`
- `progress.upload_percent`
- `progress.done_source_frames`
- `progress.total_source_frames`
- `progress.current_chunk`
- `progress.elapsed_seconds`
- `progress.estimated_remaining_seconds`

### Download the result

```bash
curl -L -o output_x2.mp4 "http://127.0.0.1:8000/v1/jobs/<job_id>/result"
```

Resume an interrupted download:

```bash
curl -L -H "Range: bytes=1048576-" -o output_x2.part \
  "http://127.0.0.1:8000/v1/jobs/<job_id>/result"
```

### Cancel a job

```bash
curl -X DELETE "http://127.0.0.1:8000/v1/jobs/<job_id>"
```

Cancellation semantics:

- an idle `uploading` job can be cancelled immediately and becomes `cancelled`
- an active `uploading` job becomes `cancelling`, then stops receiving bytes and becomes `cancelled`
- a `queued` job becomes `cancelled` immediately
- a `running` job becomes `cancelling`, then stops at the next chunk boundary and becomes `cancelled`
- completed jobs cannot be cancelled and return `409`

## HTTP API

### Endpoints

- `POST /v1/jobs`
  With `Content-Type: application/octet-stream`, uploads a video and queues a 2x upscale job. With `Content-Type: application/json`, creates an `uploading` job session from `filename` and `size_bytes`.
- `PUT /v1/jobs/<job_id>/upload`
  Uploads bytes for a job created from JSON and queues it after the declared `size_bytes` is received.
- `GET /v1/jobs/<job_id>`
  Returns job status, upload progress, render progress, ETA hints, and download readiness.
- `GET /v1/jobs/<job_id>/result`
  Downloads the finished MP4. Supports single-range `Range` requests. Returns `409` while the job is not yet complete.
- `DELETE /v1/jobs/<job_id>`
  Cancels a queued or running job.
- `GET /v1/jobs`
  Lists recent jobs and queue summary.
- `GET /healthz`
  Lightweight health check.

### Failure handling

Upload handling:

- uploads are written to a temporary `.part` file first
- one-shot uploads are still accepted by `POST /v1/jobs` with an octet-stream body
- progress-aware uploads use `POST /v1/jobs` with JSON first, then `PUT /v1/jobs/<job_id>/upload`
- while a progress-aware upload is active, `GET /v1/jobs/<job_id>` reports `input.uploaded_bytes`, `input.upload_percent`, and `progress.upload_percent`
- `DELETE /v1/jobs/<job_id>` now also works during `uploading`; idle sessions cancel immediately, while active uploads stop on the next copy-loop check
- if a one-shot upload disconnects mid-stream, the service returns an error and removes the incomplete job directory
- if a progress-aware upload disconnects mid-stream, the visible job becomes `failed`
- if the queue is full, `POST /v1/jobs` returns `429 Too Many Requests`

Render handling:

- chunk status is persisted in the manifest
- if rendering fails, the job becomes `failed`
- the response payload exposes the error text in `error`
- service restart resumes `queued` and `running` jobs with `resume=True`

Download handling:

- completed outputs support `Range` resume
- invalid byte ranges return `416 Requested Range Not Satisfiable`
- client disconnect during download does not corrupt the saved result

## CLI Workflow

### Generate a manifest

```bash
flashvsr-long-video plan \
  --input /data/input.mp4 \
  --output /data/output_x2.mp4 \
  --scale 2 \
  --work-dir /data/flashvsr_run \
  --upstream-root /path/to/FlashVSR
```

Or provide the infer script explicitly:

```bash
flashvsr-long-video plan \
  --input /data/input.mp4 \
  --output /data/output_x2.mp4 \
  --infer-script /path/to/FlashVSR/examples/WanVSR/infer_flashvsr_v1.1_tiny_long_video.py \
  --weights-dir /path/to/weights/FlashVSR-v1.1
```

The command prints the manifest JSON and writes it to `<work-dir>/manifest.json` unless `--manifest` is provided.

### Run from a manifest

```bash
flashvsr-long-video run --manifest /data/flashvsr_run/manifest.json
```

Resume a partially completed run:

```bash
flashvsr-long-video run --manifest /data/flashvsr_run/manifest.json --resume
```

If the manifest does not already store upstream paths, you can provide them at runtime:

```bash
flashvsr-long-video run \
  --manifest /data/flashvsr_run/manifest.json \
  --upstream-root /path/to/FlashVSR \
  --resume
```

## Requirements And Upstream Setup

If you do not already have a working upstream checkout, set that up first.

### 1. Clone upstream FlashVSR

```bash
git clone https://github.com/OpenImagingLab/FlashVSR
cd FlashVSR
```

### 2. Create the Python environment

The upstream project recommends Python `3.11.13`:

```bash
conda create -n flashvsr python=3.11.13
conda activate flashvsr
pip install -e .
pip install -r requirements.txt
```

### 3. Install Block-Sparse-Attention

FlashVSR depends on the Block-Sparse-Attention backend. The upstream README recommends installing it in a separate clean directory:

```bash
git clone https://github.com/mit-han-lab/Block-Sparse-Attention
cd Block-Sparse-Attention
pip install packaging
pip install ninja
python setup.py install
```

Notes:

- the build step can use a lot of memory during compilation
- the upstream README explicitly says compatibility and performance outside A100/A800/H200 are not guaranteed

### 4. Download the model weights

From the upstream repo root:

```bash
cd examples/WanVSR
git lfs install

# v1 (original)
git lfs clone https://huggingface.co/JunhaoZhuang/FlashVSR

# or v1.1 (recommended by upstream)
git lfs clone https://huggingface.co/JunhaoZhuang/FlashVSR-v1.1
```

Expected weight layout:

```text
examples/WanVSR/FlashVSR-v1.1/
  LQ_proj_in.ckpt
  TCDecoder.ckpt
  Wan2.1_VAE.pth
  diffusion_pytorch_model_streaming_dmd.safetensors
```

This wrapper only expects a valid upstream checkout plus a valid weight folder. It does not bundle either one.

## Architecture Overview

### Planning phase

`flashvsr-long-video plan` inspects the video and produces a manifest JSON.

Each chunk records:

- `source_start`, `source_end`: exact output frame range owned by the chunk
- `render_start`, `render_end`: actual source frames sent into upstream FlashVSR
- `pad_left`, `pad_right`: synthetic boundary duplication if needed
- `trim_start`, `trim_end`: how the rendered window is sliced back to the exact source chunk

The manifest is the source of truth for both execution and resume.

### Tail heuristic

FlashVSR-friendly render windows are lengths like `5, 13, 21, ...`, which follow `8n-3`.
In practice, this long-video pipeline needs at least a 21-frame render window before its internal buffered decode path emits any frames.

For long videos, a naive split can leave a tiny trailing chunk such as 1 to 8 frames. Instead of rendering that tiny tail directly, the planner can merge the tail into a larger final render window by borrowing some frames from the previous chunk, then slicing the output back down to the exact tail range.

Example:

- exact source chunks: `[0:21)`, `[21:42)`, `[42:50)`
- last source chunk is only 8 frames
- planner renders the last chunk with `render_start=29`, `render_end=50` for a 21-frame render window
- after inference, it trims the first 13 rendered frames away and keeps only `[42:50)`

### Runtime phase

`flashvsr-long-video run`:

- loads the manifest
- dynamically imports the upstream infer script
- initializes the upstream pipeline once
- renders each chunk by explicit frame indices
- writes per-chunk MP4 files
- concatenates chunk videos with `ffmpeg`
- restores original audio into the final output

Resume is index-based because each chunk already knows its exact frame ranges and output path.

## Manifest Sketch

```json
{
  "input_path": "/data/input.mp4",
  "output_path": "/data/output_x2.mp4",
  "scale": 2.0,
  "video": {
    "total_frames": 50,
    "fps_text": "25/1"
  },
  "chunks": [
    {
      "index": 2,
      "source_start": 42,
      "source_end": 50,
      "render_start": 29,
      "render_end": 50,
      "trim_start": 13,
      "trim_end": 21,
      "output_path": ".../chunks/chunk_00002.mp4"
    }
  ]
}
```

## Preview

The assets below were generated from the local sample `video.mp4` using this wrapper plus upstream `infer_flashvsr_v1.1_tiny_long_video.py`.

- input sample: `960x720`, `4007` frames, about `133.6s`
- preview output: first rendered chunk `chunk_00000.mp4`
- output chunk size: `1920x1408`
- upstream aligns to model-friendly dimensions, so the height is center-cropped rather than landing on a strict `1920x1440`

Frame 10, prepared input on the left and FlashVSR output on the right:

![Frame 10 Comparison](docs/media/frame10_compare.png)

Detail crop around the logo and title area:

![Frame 10 Detail Comparison](docs/media/frame10_detail_compare.png)

Short comparison clip for the first chunk:

[![Chunk 00000 Comparison Clip](docs/media/frame10_compare.png)](docs/media/chunk_00000_compare.mp4)

Observed on this sample:

- the large title strokes and distant mountain edges are visibly sharper
- the tiny upper-left overlay text still shows ringing and artifacting

## Assumptions

- the upstream infer script remains compatible with dynamic import
- the upstream weight layout is still `FlashVSR-v1.1/` unless `--weights-dir` is supplied
- the upstream model still expects `8n-3` render windows, plus the extra internal 4-frame padding strategy used by the reference examples, and at least 21 source frames per render window in long-video mode
- chunk videos remain compatible enough for stream-copy concat

## Limitations

- this project currently targets the upstream `infer_flashvsr_v1.1_tiny_long_video.py` flow specifically
- it does not attempt scene-aware chunking or content-aware overlap tuning
- very small entire videos still rely on boundary duplication because there is no previous context to borrow
- runtime validation in this environment is primarily unit tests; full end-to-end GPU execution still needs a real FlashVSR runtime and weights

## Development

Run tests:

```bash
pytest
```

## Repository Layout

```text
src/flashvsr_long_video_runner/
  cli.py
  manifest.py
  media.py
  planning.py
  runner.py
  service.py
  storage.py
  upstream.py
  workflow.py
tests/
```
