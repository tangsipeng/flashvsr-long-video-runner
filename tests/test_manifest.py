from pathlib import Path

from flashvsr_long_video_runner.manifest import PlannerConfig, UpstreamConfig, VideoMeta, build_manifest, load_manifest, save_manifest
from flashvsr_long_video_runner.planning import plan_chunks
from flashvsr_long_video_runner.runner import _ensure_upstream_render_window, assemble_render_frames, pending_chunk_indices


class DummyImage:
    def __init__(self, name: str):
        self.name = name

    def copy(self):
        return DummyImage(self.name + "_copy")


def test_manifest_roundtrip_preserves_explicit_ranges(tmp_path: Path):
    video = VideoMeta(
        width=1920,
        height=1080,
        total_frames=50,
        duration_seconds=2.0,
        fps_text="25/1",
        fps_float=25.0,
        has_audio=True,
    )
    planner = PlannerConfig(max_render_frames=21, tiny_tail_threshold=8, tail_merge_min_render_frames=21)
    chunks = plan_chunks(50, max_render_frames=21, tiny_tail_threshold=8, tail_merge_min_render_frames=21)
    manifest = build_manifest(
        input_path=tmp_path / "input.mp4",
        output_path=tmp_path / "output.mp4",
        work_dir=tmp_path / "work",
        scale=2.0,
        video=video,
        planner=planner,
        chunks=chunks,
        upstream=UpstreamConfig(infer_script="/upstream/infer.py", weights_dir="/weights"),
    )
    manifest_path = tmp_path / "manifest.json"
    save_manifest(manifest, manifest_path)
    loaded = load_manifest(manifest_path)

    assert loaded.chunks[-1].source_start == 42
    assert loaded.chunks[-1].source_end == 50
    assert loaded.chunks[-1].render_start == 29
    assert loaded.chunks[-1].render_end == 50
    assert loaded.chunks[-1].trim_start == 13
    assert loaded.chunks[-1].trim_end == 21
    assert loaded.upstream.infer_script == "/upstream/infer.py"


def test_assemble_render_frames_applies_padding():
    actual = [DummyImage("a"), DummyImage("b"), DummyImage("c")]
    chunk = type(
        "Chunk",
        (),
        {
            "index": 0,
            "pad_left": 1,
            "pad_right": 2,
            "render_length": 6,
        },
    )()
    frames = assemble_render_frames(chunk, actual)
    assert len(frames) == 6
    assert [frame.name for frame in frames] == ["a_copy", "a", "b", "c", "c_copy", "c_copy"]


def test_ensure_upstream_render_window_expands_legacy_short_tail():
    frames = [DummyImage(f"f{i}") for i in range(13)]
    chunk = type(
        "Chunk",
        (),
        {
            "trim_start": 11,
            "trim_end": 13,
        },
    )()
    expanded, trim_start, trim_end = _ensure_upstream_render_window(chunk, frames)
    assert len(expanded) == 21
    assert trim_start == 19
    assert trim_end == 21
    assert expanded[0].name == "f0_copy"
    assert expanded[8].name == "f0"
    assert expanded[-1].name == "f12"


def test_pending_chunk_indices_are_index_based(tmp_path: Path):
    video = VideoMeta(
        width=1920,
        height=1080,
        total_frames=42,
        duration_seconds=2.0,
        fps_text="25/1",
        fps_float=25.0,
        has_audio=False,
    )
    planner = PlannerConfig()
    manifest = build_manifest(
        input_path=tmp_path / "input.mp4",
        output_path=tmp_path / "output.mp4",
        work_dir=tmp_path / "work",
        scale=2.0,
        video=video,
        planner=planner,
        chunks=plan_chunks(42),
    )
    first_output = Path(manifest.chunks[0].output_path)
    first_output.parent.mkdir(parents=True, exist_ok=True)
    first_output.write_bytes(b"done")

    assert pending_chunk_indices(manifest, resume=True) == [1]
    assert pending_chunk_indices(manifest, resume=False) == [0, 1]
