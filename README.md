# flashvsr-long-video-runner

[中文说明](README.zh-CN.md)

A small, open-source-friendly wrapper around an **existing upstream FlashVSR checkout** for running long videos more safely.

This repository **does not vendor FlashVSR weights or modify the upstream project**. Instead, it:

- probes an input video with `ffprobe`
- generates an explicit chunk manifest with frame ranges
- renders chunks through the upstream `infer_flashvsr_v1.1_tiny_long_video.py`
- supports resume by **chunk index + chunk file existence**, not iterator position
- concatenates chunk videos and re-muxes the original audio

The main goal is to make long-video execution less fragile and easier to resume after interruption.

## Why this exists

The upstream long-video example is great for proving the idea, but long runs get awkward when you need:

- deterministic chunk plans
- explicit frame bookkeeping
- safe resume after a crash or manual stop
- less fragile handling for very short trailing tails
- a standalone repo that can be shared without bundling private weights

## Architecture

### 1. Planning phase

`flashvsr-long-video plan` inspects the video and produces a manifest JSON.

Each chunk records:

- `source_start`, `source_end`: exact output frame range this chunk owns
- `render_start`, `render_end`: actual source frames fed into upstream FlashVSR
- `pad_left`, `pad_right`: synthetic boundary duplication if needed
- `trim_start`, `trim_end`: how the rendered window is sliced back to the exact source chunk

That means the manifest is the source of truth for both execution and resume.

### 2. Tail heuristic

FlashVSR-friendly render windows are lengths like `5, 13, 21, ...` (`8n-3`).

For long videos, a naive split can leave a tiny trailing chunk such as 1–8 frames. Instead of rendering that tiny tail directly, the planner can **merge the tail into a larger final render window** by borrowing some frames from the previous chunk, then slice the rendered output back down to the exact tail range.

Example:

- exact source chunks: `[0:21)`, `[21:42)`, `[42:50)`
- last source chunk is only 8 frames
- planner renders the last chunk with `render_start=37`, `render_end=50` (13 frames)
- after inference, it trims the first 5 rendered frames away and keeps only frames `[42:50)`

This keeps source chunk ownership explicit while avoiding fragile tiny tail renders.

### 3. Runtime phase

`flashvsr-long-video run`:

- loads the manifest
- dynamically imports the upstream infer script
- initializes the upstream pipeline once
- renders each chunk by explicit frame indices
- writes per-chunk MP4 files
- concatenates them with `ffmpeg`
- restores audio from the original input

Resume is **index-based** because each chunk already knows its exact frame ranges and output path.

## Requirements

- Python 3.10+
- `ffmpeg` + `ffprobe` on `PATH`
- a working upstream FlashVSR environment with GPU dependencies installed
- an upstream checkout such as:
  - `/path/to/FlashVSR/examples/WanVSR/infer_flashvsr_v1.1_tiny_long_video.py`
  - `/path/to/FlashVSR/examples/WanVSR/FlashVSR-v1.1/`

This wrapper intentionally keeps its own Python dependencies light. GPU/runtime dependencies still come from the upstream FlashVSR environment.

## Install the upstream FlashVSR stack

If you do not already have a working upstream checkout, set that up first. The steps below are summarized from the official FlashVSR repository's installation flow.

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

Note:

- the build step can use a lot of memory during compilation
- the upstream README explicitly says compatibility and performance outside A100/A800/H200 are not guaranteed

### 4. Download the original model weights

From the upstream repo root:

```bash
cd examples/WanVSR
git lfs install

# v1 (original)
git lfs clone https://huggingface.co/JunhaoZhuang/FlashVSR

# or v1.1 (recommended by upstream)
git lfs clone https://huggingface.co/JunhaoZhuang/FlashVSR-v1.1
```

Expected layout:

```text
examples/WanVSR/FlashVSR-v1.1/
  LQ_proj_in.ckpt
  TCDecoder.ckpt
  Wan2.1_VAE.pth
  diffusion_pytorch_model_streaming_dmd.safetensors
```

The wrapper in this repository only expects a valid upstream checkout plus one of these weight folders. It does not bundle either one.

## Install this wrapper

```bash
cd flashvsr-long-video-runner
pip install -e .
```

## Usage

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

## Preview

The assets below were generated from the local sample `video.mp4` using this wrapper plus upstream `infer_flashvsr_v1.1_tiny_long_video.py`.

- input sample: `960x720`, `4007` frames, about `133.6s`
- preview output: first rendered chunk `chunk_00000.mp4`
- output chunk size: `1920x1408`
- note: upstream aligns to model-friendly dimensions, so the height is center-cropped instead of landing on a strict `1920x1440`

Frame 10, prepared input on the left and FlashVSR output on the right:

![Frame 10 Comparison](docs/media/frame10_compare.png)

Detail crop around the logo and title area:

![Frame 10 Detail Comparison](docs/media/frame10_detail_compare.png)

Short comparison clip for the first chunk:

[![Chunk 00000 Comparison Clip](docs/media/frame10_compare.png)](docs/media/chunk_00000_compare.mp4)

Observed on this sample:

- the large title strokes and distant mountain edges are visibly sharper
- the tiny upper-left overlay text still shows ringing / artifacting

## Manifest sketch

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
      "render_start": 37,
      "render_end": 50,
      "trim_start": 5,
      "trim_end": 13,
      "output_path": ".../chunks/chunk_00002.mp4"
    }
  ]
}
```

## Assumptions

- the upstream infer script remains compatible with dynamic import
- the upstream weight layout is still `FlashVSR-v1.1/` unless `--weights-dir` is supplied
- the upstream model still expects render windows shaped like `8n-3`, with the extra internal 4-frame padding strategy used by the reference examples
- chunk videos are compatible enough for stream-copy concat

## Limitations

- This project currently targets the upstream `infer_flashvsr_v1.1_tiny_long_video.py` flow specifically.
- It does not attempt scene-aware chunking or content-aware overlap tuning.
- Very small entire videos still rely on boundary duplication because there is no previous context to borrow.
- Runtime validation was limited to planner/unit tests in this environment; end-to-end GPU execution still needs a real FlashVSR runtime + weights.

## Development

Run tests:

```bash
pytest
```

## Repository layout

```text
src/flashvsr_long_video_runner/
  cli.py
  manifest.py
  media.py
  planning.py
  runner.py
  upstream.py
tests/
```
