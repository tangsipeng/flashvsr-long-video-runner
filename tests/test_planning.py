import pytest

from flashvsr_long_video_runner.planning import plan_chunks, valid_render_lengths


def test_valid_render_lengths_default():
    assert valid_render_lengths(21) == [5, 13, 21]


def test_plan_chunks_covers_all_source_frames_without_gaps():
    chunks = plan_chunks(50, max_render_frames=21, tiny_tail_threshold=8, tail_merge_min_render_frames=21)
    assert [(chunk.source_start, chunk.source_end) for chunk in chunks] == [(0, 21), (21, 42), (42, 50)]
    assert sum(chunk.source_length for chunk in chunks) == 50
    assert chunks[-1].render_start == 29
    assert chunks[-1].render_end == 50
    assert chunks[-1].render_length == 21
    assert chunks[-1].trim_start == 13
    assert chunks[-1].trim_end == 21


def test_tiny_tail_uses_larger_render_window():
    chunks = plan_chunks(43, max_render_frames=21, tiny_tail_threshold=8, tail_merge_min_render_frames=21)
    tail = chunks[-1]
    assert tail.source_length == 1
    assert tail.render_length == 21
    assert tail.render_start == 22
    assert tail.render_end == 43
    assert tail.trim_start == 20
    assert tail.trim_end == 21


def test_single_short_video_pads_right_instead_of_left():
    chunks = plan_chunks(2, max_render_frames=21, tiny_tail_threshold=8, tail_merge_min_render_frames=21)
    chunk = chunks[0]
    assert chunk.source_start == 0
    assert chunk.source_end == 2
    assert chunk.render_start == 0
    assert chunk.render_end == 2
    assert chunk.pad_left == 0
    assert chunk.pad_right == 19
    assert chunk.render_length == 21
    assert chunk.trim_start == 0
    assert chunk.trim_end == 2


def test_plan_chunks_rejects_shorter_than_pipeline_minimum():
    with pytest.raises(ValueError, match="at least 21"):
        plan_chunks(50, max_render_frames=13, tiny_tail_threshold=8, tail_merge_min_render_frames=13)
