from __future__ import annotations

import gc
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable

import imageio.v2 as imageio
from PIL import Image

from .manifest import ChunkManifest, Manifest, load_manifest, save_manifest
from .media import audio_exists, concat_videos, mux_audio
from .planning import MIN_LONG_PIPELINE_RENDER_FRAMES
from .upstream import load_upstream_module, preload_torch_libs, upstream_runtime_dir


class RunError(RuntimeError):
    pass


class RunCancelled(RunError):
    pass


@dataclass(frozen=True)
class RenderConfig:
    prompt: str = ""
    negative_prompt: str = ""
    cfg_scale: float = 1.0
    num_inference_steps: int = 1
    seed: int = 0
    kv_ratio: float = 3.0
    local_range: int = 11
    color_fix: bool = True
    output_quality: int = 5
    topk_ratio_multiplier: float = 2.0
    is_full_block: bool = False
    if_buffer: bool = True


def chunk_output_exists(chunk: ChunkManifest) -> bool:
    output = Path(chunk.output_path)
    return output.exists() and output.stat().st_size > 0


def pending_chunk_indices(manifest: Manifest, *, resume: bool) -> list[int]:
    pending: list[int] = []
    for chunk in manifest.chunks:
        if resume and chunk_output_exists(chunk):
            continue
        pending.append(chunk.index)
    return pending


def _clone_frame(frame: Image.Image) -> Image.Image:
    return frame.copy()


def assemble_render_frames(chunk: ChunkManifest, actual_frames: list[Image.Image]) -> list[Image.Image]:
    if not actual_frames:
        raise RunError(f"Chunk {chunk.index} has no actual frames to render")
    frames: list[Image.Image] = []
    if chunk.pad_left:
        frames.extend(_clone_frame(actual_frames[0]) for _ in range(chunk.pad_left))
    frames.extend(actual_frames)
    if chunk.pad_right:
        frames.extend(_clone_frame(actual_frames[-1]) for _ in range(chunk.pad_right))
    if len(frames) != chunk.render_length:
        raise RunError(
            f"Chunk {chunk.index} assembled render length {len(frames)} != manifest render_length {chunk.render_length}"
        )
    return frames


def prepare_chunk_tensor(frames: list[Image.Image], upstream_module, scale: float):
    w0, h0 = frames[0].size
    _, _, target_width, target_height = upstream_module.compute_scaled_and_target_dims(
        w0, h0, scale=scale, multiple=128
    )
    padded = frames + [frames[-1]] * 4
    tensors = []
    for img in padded:
        img_out = upstream_module.upscale_then_center_crop(
            img, scale=scale, tW=target_width, tH=target_height
        )
        tensors.append(upstream_module.pil_to_tensor_neg1_1(img_out, upstream_module.torch.bfloat16, "cpu"))
    num_frames = len(tensors)
    lq_video = upstream_module.torch.stack(tensors, 0).permute(1, 0, 2, 3).unsqueeze(0)
    return lq_video, target_height, target_width, num_frames


def _ensure_upstream_render_window(
    chunk: ChunkManifest, render_frames: list[Image.Image]
) -> tuple[list[Image.Image], int, int]:
    if len(render_frames) >= MIN_LONG_PIPELINE_RENDER_FRAMES:
        return render_frames, chunk.trim_start, chunk.trim_end

    # Legacy manifests may contain 5/13-frame windows; keep their trim alignment while padding to the pipeline minimum.
    extra = MIN_LONG_PIPELINE_RENDER_FRAMES - len(render_frames)
    prepend = min(extra, chunk.trim_start)
    append = extra - prepend

    expanded: list[Image.Image] = []
    if prepend:
        expanded.extend(_clone_frame(render_frames[0]) for _ in range(prepend))
    expanded.extend(render_frames)
    if append:
        expanded.extend(_clone_frame(render_frames[-1]) for _ in range(append))
    return expanded, chunk.trim_start + prepend, chunk.trim_end + prepend


def _mark_chunk_status(manifest: Manifest, chunk_index: int, status: str, manifest_path: str | Path) -> None:
    manifest.chunks[chunk_index].status = status
    save_manifest(manifest, manifest_path)


def render_chunk(
    reader,
    chunk: ChunkManifest,
    upstream_module,
    pipe,
    scale: float,
    fps: float,
    render_config: RenderConfig,
) -> None:
    actual_frames = [Image.fromarray(reader.get_data(i)).convert("RGB") for i in range(chunk.render_start, chunk.render_end)]
    render_frames = assemble_render_frames(chunk, actual_frames)
    render_frames, trim_start, trim_end = _ensure_upstream_render_window(chunk, render_frames)

    upstream_module.torch.cuda.empty_cache()
    upstream_module.torch.cuda.ipc_collect()
    gc.collect()

    lq_video, target_height, target_width, num_frames = prepare_chunk_tensor(render_frames, upstream_module, scale)
    video = pipe(
        prompt=render_config.prompt,
        negative_prompt=render_config.negative_prompt,
        cfg_scale=render_config.cfg_scale,
        num_inference_steps=render_config.num_inference_steps,
        seed=render_config.seed,
        LQ_video=lq_video,
        num_frames=num_frames,
        height=target_height,
        width=target_width,
        is_full_block=render_config.is_full_block,
        if_buffer=render_config.if_buffer,
        topk_ratio=render_config.topk_ratio_multiplier * 768 * 1280 / (target_height * target_width),
        kv_ratio=render_config.kv_ratio,
        local_range=render_config.local_range,
        color_fix=render_config.color_fix,
    )
    rendered_frames = upstream_module.tensor2video(video)[: len(render_frames)]
    trimmed = rendered_frames[trim_start:trim_end]
    if len(trimmed) != chunk.source_length:
        raise RunError(
            f"Chunk {chunk.index} trim produced {len(trimmed)} frames, expected {chunk.source_length}"
        )

    out_path = Path(chunk.output_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    upstream_module.save_video(trimmed, str(out_path), fps=fps, quality=render_config.output_quality)


def _all_chunk_outputs(manifest: Manifest) -> list[str]:
    return [chunk.output_path for chunk in sorted(manifest.chunks, key=lambda item: item.index)]


def run_manifest(
    manifest_path: str | Path,
    *,
    upstream_root: str | Path | None = None,
    infer_script: str | Path | None = None,
    weights_dir: str | Path | None = None,
    resume: bool = False,
    should_stop: Callable[[], bool] | None = None,
    render_config: RenderConfig | None = None,
) -> Manifest:
    manifest_path = Path(manifest_path).resolve()
    manifest = load_manifest(manifest_path)
    infer_script = infer_script or manifest.upstream.infer_script
    weights_dir = weights_dir or manifest.upstream.weights_dir
    if upstream_root and not infer_script:
        from .upstream import resolve_infer_script

        infer_script = resolve_infer_script(upstream_root, None)
    if not infer_script:
        raise RunError("Manifest does not contain an infer script and none was provided")

    manifest.upstream.infer_script = str(Path(infer_script).resolve())
    if weights_dir:
        manifest.upstream.weights_dir = str(Path(weights_dir).resolve())
    effective_render_config = render_config or RenderConfig()
    if resume:
        for chunk in manifest.chunks:
            if chunk_output_exists(chunk):
                chunk.status = "done"
    save_manifest(manifest, manifest_path)

    preload_torch_libs()
    upstream_module = load_upstream_module(manifest.upstream.infer_script)

    with upstream_runtime_dir(manifest.upstream.infer_script, manifest.upstream.weights_dir):
        pipe = upstream_module.init_pipeline()
        reader = imageio.get_reader(manifest.input_path)
        try:
            for chunk_index in pending_chunk_indices(manifest, resume=resume):
                if should_stop and should_stop():
                    raise RunCancelled("Job cancellation requested")
                chunk = manifest.chunks[chunk_index]
                _mark_chunk_status(manifest, chunk_index, "running", manifest_path)
                try:
                    render_chunk(
                        reader,
                        chunk,
                        upstream_module,
                        pipe,
                        manifest.scale,
                        manifest.video.fps_float,
                        effective_render_config,
                    )
                except Exception:
                    _mark_chunk_status(manifest, chunk_index, "failed", manifest_path)
                    raise
                _mark_chunk_status(manifest, chunk_index, "done", manifest_path)
        finally:
            try:
                reader.close()
            except Exception:
                pass

    if should_stop and should_stop():
        raise RunCancelled("Job cancellation requested")

    chunk_outputs = _all_chunk_outputs(manifest)
    missing = [path for path in chunk_outputs if not Path(path).exists()]
    if missing:
        raise RunError(f"Missing rendered chunk outputs: {missing}")

    work_dir = Path(manifest.work_dir)
    merged_noaudio = work_dir / "merged_noaudio.mp4"
    concat_file = work_dir / "concat.txt"
    concat_videos(chunk_outputs, merged_noaudio, concat_file=concat_file)

    final_output = Path(manifest.output_path)
    if audio_exists(manifest.input_path):
        mux_audio(merged_noaudio, manifest.input_path, final_output)
    else:
        final_output.parent.mkdir(parents=True, exist_ok=True)
        merged_noaudio.replace(final_output)

    manifest.merged_video_path = str(final_output.resolve())
    save_manifest(manifest, manifest_path)
    return manifest
