from __future__ import annotations

from pathlib import Path

from .manifest import Manifest, PlannerConfig, UpstreamConfig, build_manifest, save_manifest
from .media import probe_video
from .planning import plan_chunks
from .upstream import resolve_infer_script, resolve_weights_dir


def default_manifest_path(work_dir: str | Path) -> Path:
    return (Path(work_dir).expanduser().resolve() / "manifest.json").resolve()


def plan_video_job(
    *,
    input_path: str | Path,
    output_path: str | Path,
    work_dir: str | Path,
    scale: float,
    planner: PlannerConfig,
    manifest_path: str | Path | None = None,
    upstream_root: str | Path | None = None,
    infer_script: str | Path | None = None,
    weights_dir: str | Path | None = None,
) -> Manifest:
    resolved_input = Path(input_path).expanduser().resolve()
    resolved_output = Path(output_path).expanduser().resolve()
    resolved_work_dir = Path(work_dir).expanduser().resolve()
    resolved_manifest = (
        Path(manifest_path).expanduser().resolve()
        if manifest_path is not None
        else default_manifest_path(resolved_work_dir)
    )

    video = probe_video(resolved_input)
    chunks = plan_chunks(
        video.total_frames,
        max_render_frames=planner.max_render_frames,
        tiny_tail_threshold=planner.tiny_tail_threshold,
        tail_merge_min_render_frames=planner.tail_merge_min_render_frames,
    )

    resolved_infer_script = None
    resolved_weights_dir = None
    if infer_script or upstream_root:
        resolved_infer_script = resolve_infer_script(upstream_root, infer_script)
        resolved_weights_dir = resolve_weights_dir(resolved_infer_script, weights_dir)

    manifest = build_manifest(
        input_path=resolved_input,
        output_path=resolved_output,
        work_dir=resolved_work_dir,
        scale=scale,
        video=video,
        planner=planner,
        chunks=chunks,
        upstream=UpstreamConfig(
            infer_script=str(resolved_infer_script) if resolved_infer_script else None,
            weights_dir=str(resolved_weights_dir) if resolved_weights_dir else None,
        ),
    )
    save_manifest(manifest, resolved_manifest)
    return manifest
