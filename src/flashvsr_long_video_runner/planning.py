from __future__ import annotations

from dataclasses import dataclass


MIN_LONG_PIPELINE_RENDER_FRAMES = 21


@dataclass(frozen=True)
class ChunkPlan:
    index: int
    source_start: int
    source_end: int
    render_start: int
    render_end: int
    pad_left: int
    pad_right: int
    trim_start: int

    @property
    def source_length(self) -> int:
        return self.source_end - self.source_start

    @property
    def render_length(self) -> int:
        return (self.render_end - self.render_start) + self.pad_left + self.pad_right

    @property
    def trim_end(self) -> int:
        return self.trim_start + self.source_length

    def to_dict(self) -> dict:
        return {
            "index": self.index,
            "source_start": self.source_start,
            "source_end": self.source_end,
            "source_length": self.source_length,
            "render_start": self.render_start,
            "render_end": self.render_end,
            "render_length": self.render_length,
            "pad_left": self.pad_left,
            "pad_right": self.pad_right,
            "trim_start": self.trim_start,
            "trim_end": self.trim_end,
        }


def valid_render_lengths(max_render_frames: int) -> list[int]:
    if max_render_frames < 5:
        raise ValueError("max_render_frames must be at least 5")
    lengths = [length for length in range(5, max_render_frames + 1, 8)]
    if not lengths or lengths[-1] != max_render_frames:
        raise ValueError(
            "max_render_frames must match an upstream-friendly FlashVSR window length (5, 13, 21, ...)."
        )
    return lengths


def choose_render_length(
    source_length: int,
    valid_lengths: list[int],
    *,
    prefer_tail_merge: bool,
    tail_merge_min_render_frames: int,
) -> int:
    if source_length <= 0:
        raise ValueError("source_length must be positive")
    target = max(source_length, MIN_LONG_PIPELINE_RENDER_FRAMES)
    if prefer_tail_merge:
        target = max(target, tail_merge_min_render_frames)
    for length in valid_lengths:
        if length >= target:
            return length
    raise ValueError(
        f"Cannot find render length for source_length={source_length} within valid lengths {valid_lengths}"
    )


def plan_chunks(
    total_frames: int,
    *,
    max_render_frames: int = 21,
    tiny_tail_threshold: int = 8,
    tail_merge_min_render_frames: int = MIN_LONG_PIPELINE_RENDER_FRAMES,
) -> list[ChunkPlan]:
    if total_frames <= 0:
        raise ValueError("total_frames must be positive")
    if max_render_frames < MIN_LONG_PIPELINE_RENDER_FRAMES:
        raise ValueError(
            f"max_render_frames must be at least {MIN_LONG_PIPELINE_RENDER_FRAMES} for the upstream FlashVSR long-video pipeline"
        )
    valid_lengths = valid_render_lengths(max_render_frames)
    if tail_merge_min_render_frames not in valid_lengths:
        raise ValueError(
            f"tail_merge_min_render_frames={tail_merge_min_render_frames} must be one of {valid_lengths}"
        )
    if tail_merge_min_render_frames < MIN_LONG_PIPELINE_RENDER_FRAMES:
        raise ValueError(
            f"tail_merge_min_render_frames must be at least {MIN_LONG_PIPELINE_RENDER_FRAMES} for the upstream FlashVSR long-video pipeline"
        )

    chunks: list[ChunkPlan] = []
    full_chunks, remainder = divmod(total_frames, max_render_frames)
    source_start = 0

    for index in range(full_chunks):
        source_end = source_start + max_render_frames
        chunks.append(
            ChunkPlan(
                index=index,
                source_start=source_start,
                source_end=source_end,
                render_start=source_start,
                render_end=source_end,
                pad_left=0,
                pad_right=0,
                trim_start=0,
            )
        )
        source_start = source_end

    if remainder == 0 and chunks:
        return chunks

    source_end = total_frames
    source_length = source_end - source_start
    is_only_chunk = not chunks
    prefer_tail_merge = (not is_only_chunk) and source_length <= tiny_tail_threshold
    render_length = choose_render_length(
        source_length,
        valid_lengths,
        prefer_tail_merge=prefer_tail_merge,
        tail_merge_min_render_frames=tail_merge_min_render_frames,
    )
    extra = render_length - source_length

    if is_only_chunk:
        use_left_context = 0
        pad_left = 0
        pad_right = extra
    else:
        use_left_context = min(extra, source_start)
        pad_left = 0
        pad_right = extra - use_left_context

    render_start = source_start - use_left_context
    render_end = source_end
    trim_start = pad_left + use_left_context

    chunks.append(
        ChunkPlan(
            index=len(chunks),
            source_start=source_start,
            source_end=source_end,
            render_start=render_start,
            render_end=render_end,
            pad_left=pad_left,
            pad_right=pad_right,
            trim_start=trim_start,
        )
    )
    return chunks
