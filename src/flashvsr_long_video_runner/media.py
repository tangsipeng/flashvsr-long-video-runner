from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

from .manifest import VideoMeta


class ExternalToolError(RuntimeError):
    pass


def ensure_ffmpeg_tools() -> None:
    for binary in ("ffmpeg", "ffprobe"):
        if shutil.which(binary) is None:
            raise ExternalToolError(f"Required binary not found on PATH: {binary}")


def _run(cmd: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, check=True, capture_output=True, text=True)


def probe_video(path: str | Path) -> VideoMeta:
    ensure_ffmpeg_tools()
    src = Path(path)
    proc = _run(
        [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-count_frames",
            "-show_entries",
            "stream=nb_read_frames,nb_frames,r_frame_rate,avg_frame_rate,width,height",
            "-show_entries",
            "format=duration",
            "-of",
            "json",
            str(src),
        ]
    )
    data = json.loads(proc.stdout)
    stream = data["streams"][0]
    fps_text = stream.get("avg_frame_rate") or stream.get("r_frame_rate") or "30/1"
    numerator, denominator = fps_text.split("/")
    fps_float = float(numerator) / float(denominator)
    total_frames = int(stream.get("nb_read_frames") or stream.get("nb_frames"))
    duration_seconds = float(data["format"].get("duration") or 0.0)
    return VideoMeta(
        width=int(stream["width"]),
        height=int(stream["height"]),
        total_frames=total_frames,
        duration_seconds=duration_seconds,
        fps_text=fps_text,
        fps_float=fps_float,
        has_audio=audio_exists(src),
    )


def audio_exists(path: str | Path) -> bool:
    ensure_ffmpeg_tools()
    src = Path(path)
    proc = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "a:0",
            "-show_entries",
            "stream=index",
            "-of",
            "csv=p=0",
            str(src),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    return bool(proc.stdout.strip())


def write_concat_file(video_paths: list[str | Path], concat_file: str | Path) -> Path:
    target = Path(concat_file)
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("w", encoding="utf-8") as handle:
        for path in video_paths:
            handle.write(f"file '{Path(path).resolve().as_posix()}'\n")
    return target


def concat_videos(video_paths: list[str | Path], output_path: str | Path, concat_file: str | Path | None = None) -> Path:
    ensure_ffmpeg_tools()
    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    concat_path = write_concat_file(video_paths, concat_file or output.with_suffix(".concat.txt"))
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            str(concat_path),
            "-c",
            "copy",
            str(output),
        ],
        check=True,
    )
    return output


def mux_audio(video_path: str | Path, audio_source_path: str | Path, output_path: str | Path) -> Path:
    ensure_ffmpeg_tools()
    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-i",
            str(Path(video_path).resolve()),
            "-i",
            str(Path(audio_source_path).resolve()),
            "-map",
            "0:v:0",
            "-map",
            "1:a?",
            "-c:v",
            "copy",
            "-c:a",
            "aac",
            "-shortest",
            str(output),
        ],
        check=True,
    )
    return output
