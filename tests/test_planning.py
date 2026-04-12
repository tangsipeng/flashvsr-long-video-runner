from flashvsr_long_video_runner.planning import plan_chunks, valid_render_lengths


def test_valid_render_lengths_default():
    assert valid_render_lengths(21) == [5, 13, 21]


def test_plan_chunks_covers_all_source_frames_without_gaps():
    chunks = plan_chunks(50, max_render_frames=21, tiny_tail_threshold=8, tail_merge_min_render_frames=13)
    assert [(chunk.source_start, chunk.source_end) for chunk in chunks] == [(0, 21), (21, 42), (42, 50)]
    assert sum(chunk.source_length for chunk in chunks) == 50
    assert chunks[-1].render_start == 37
    assert chunks[-1].render_end == 50
    assert chunks[-1].render_length == 13
    assert chunks[-1].trim_start == 5
    assert chunks[-1].trim_end == 13


def test_tiny_tail_uses_larger_render_window():
    chunks = plan_chunks(43, max_render_frames=21, tiny_tail_threshold=8, tail_merge_min_render_frames=13)
    tail = chunks[-1]
    assert tail.source_length == 1
    assert tail.render_length == 13
    assert tail.render_start == 30
    assert tail.render_end == 43
    assert tail.trim_start == 12
    assert tail.trim_end == 13


def test_single_short_video_pads_right_instead_of_left():
    chunks = plan_chunks(2, max_render_frames=21, tiny_tail_threshold=8, tail_merge_min_render_frames=13)
    chunk = chunks[0]
    assert chunk.source_start == 0
    assert chunk.source_end == 2
    assert chunk.render_start == 0
    assert chunk.render_end == 2
    assert chunk.pad_left == 0
    assert chunk.pad_right == 3
    assert chunk.render_length == 5
    assert chunk.trim_start == 0
    assert chunk.trim_end == 2
