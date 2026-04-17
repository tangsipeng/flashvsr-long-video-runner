from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .planning import ChunkPlan, MIN_LONG_PIPELINE_RENDER_FRAMES
from .storage import write_json_atomic


@dataclass
class VideoMeta:
    width: int
    height: int
    total_frames: int
    duration_seconds: float
    fps_text: str
    fps_float: float
    has_audio: bool


@dataclass
class PlannerConfig:
    max_render_frames: int = 21
    tiny_tail_threshold: int = 8
    tail_merge_min_render_frames: int = MIN_LONG_PIPELINE_RENDER_FRAMES


@dataclass
class UpstreamConfig:
    infer_script: str | None = None
    weights_dir: str | None = None


@dataclass
class ChunkManifest:
    index: int
    source_start: int
    source_end: int
    source_length: int
    render_start: int
    render_end: int
    render_length: int
    pad_left: int
    pad_right: int
    trim_start: int
    trim_end: int
    output_path: str
    status: str = "pending"
    notes: list[str] = field(default_factory=list)


@dataclass
class Manifest:
    version: int
    created_at: str
    input_path: str
    output_path: str
    work_dir: str
    scale: float
    video: VideoMeta
    planner: PlannerConfig
    upstream: UpstreamConfig
    chunks: list[ChunkManifest]
    merged_video_path: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def build_manifest(
    *,
    input_path: Path,
    output_path: Path,
    work_dir: Path,
    scale: float,
    video: VideoMeta,
    planner: PlannerConfig,
    chunks: list[ChunkPlan],
    upstream: UpstreamConfig | None = None,
) -> Manifest:
    chunk_dir = work_dir / "chunks"
    manifest_chunks: list[ChunkManifest] = []
    for chunk in chunks:
        notes: list[str] = []
        if chunk.render_start != chunk.source_start or chunk.pad_left or chunk.pad_right:
            notes.append("tail_context_or_padding")
        manifest_chunks.append(
            ChunkManifest(
                index=chunk.index,
                source_start=chunk.source_start,
                source_end=chunk.source_end,
                source_length=chunk.source_length,
                render_start=chunk.render_start,
                render_end=chunk.render_end,
                render_length=chunk.render_length,
                pad_left=chunk.pad_left,
                pad_right=chunk.pad_right,
                trim_start=chunk.trim_start,
                trim_end=chunk.trim_end,
                output_path=str((chunk_dir / f"chunk_{chunk.index:05d}.mp4").resolve()),
                notes=notes,
            )
        )
    return Manifest(
        version=1,
        created_at=utc_now_iso(),
        input_path=str(input_path.resolve()),
        output_path=str(output_path.resolve()),
        work_dir=str(work_dir.resolve()),
        scale=scale,
        video=video,
        planner=planner,
        upstream=upstream or UpstreamConfig(),
        chunks=manifest_chunks,
    )


def save_manifest(manifest: Manifest, path: str | Path) -> None:
    write_json_atomic(path, manifest.to_dict(), ensure_ascii=False, indent=2)


def _coerce_dataclass(cls: type, payload: dict[str, Any]):
    return cls(**payload)


def load_manifest(path: str | Path) -> Manifest:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    return Manifest(
        version=data["version"],
        created_at=data["created_at"],
        input_path=data["input_path"],
        output_path=data["output_path"],
        work_dir=data["work_dir"],
        scale=data["scale"],
        video=_coerce_dataclass(VideoMeta, data["video"]),
        planner=_coerce_dataclass(PlannerConfig, data["planner"]),
        upstream=_coerce_dataclass(UpstreamConfig, data.get("upstream", {})),
        chunks=[_coerce_dataclass(ChunkManifest, item) for item in data["chunks"]],
        merged_video_path=data.get("merged_video_path"),
    )
