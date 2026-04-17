#!/usr/bin/env python3
from __future__ import annotations

import argparse
import itertools
import json
import re
import subprocess
import sys
import time
from dataclasses import asdict
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from flashvsr_long_video_runner.manifest import PlannerConfig
from flashvsr_long_video_runner.media import probe_video
from flashvsr_long_video_runner.runner import RenderConfig, run_manifest
from flashvsr_long_video_runner.workflow import default_manifest_path, plan_video_job


def _parse_csv_ints(value: str) -> list[int]:
    values = [int(item.strip()) for item in value.split(",") if item.strip()]
    if not values:
        raise argparse.ArgumentTypeError("expected at least one integer")
    return values


def _parse_csv_floats(value: str) -> list[float]:
    values = [float(item.strip()) for item in value.split(",") if item.strip()]
    if not values:
        raise argparse.ArgumentTypeError("expected at least one float")
    return values


def _slugify(text: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", text).strip("-")
    return slug or "case"


def _extract_subclip(input_path: Path, output_path: Path, *, start: float, duration: float) -> Path:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-i",
            str(input_path),
            "-ss",
            f"{start:.3f}",
            "-t",
            f"{duration:.3f}",
            "-map",
            "0:v:0",
            "-an",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-crf",
            "0",
            str(output_path),
        ],
        check=True,
    )
    return output_path


def _build_cases(args) -> list[tuple[str, PlannerConfig, RenderConfig]]:
    cases: list[tuple[str, PlannerConfig, RenderConfig]] = []
    for max_render_frames, num_inference_steps, output_quality, local_range, kv_ratio in itertools.product(
        args.max_render_frames,
        args.num_inference_steps,
        args.output_quality,
        args.local_range,
        args.kv_ratio,
    ):
        tail_merge = args.tail_merge_min_render_frames or max_render_frames
        planner = PlannerConfig(
            max_render_frames=max_render_frames,
            tiny_tail_threshold=args.tiny_tail_threshold,
            tail_merge_min_render_frames=tail_merge,
        )
        render = RenderConfig(
            num_inference_steps=num_inference_steps,
            output_quality=output_quality,
            seed=args.seed,
            local_range=local_range,
            kv_ratio=kv_ratio,
            cfg_scale=args.cfg_scale,
            color_fix=not args.no_color_fix,
            topk_ratio_multiplier=args.topk_ratio_multiplier,
        )
        case_name = _slugify(
            f"mrf{max_render_frames}_steps{num_inference_steps}_q{output_quality}_lr{local_range}_kv{kv_ratio:g}"
        )
        cases.append((case_name, planner, render))
    return cases


def main() -> int:
    parser = argparse.ArgumentParser(description="Run a small FlashVSR parameter matrix on a chosen source clip")
    parser.add_argument("--input", required=True, help="Source video path")
    parser.add_argument("--output-root", required=True, help="Directory for extracted clips, manifests, and outputs")
    parser.add_argument("--scale", type=float, default=2.0)
    parser.add_argument("--upstream-root", help="Path to upstream FlashVSR checkout")
    parser.add_argument("--infer-script", help="Path to upstream infer script")
    parser.add_argument("--weights-dir", help="Path to FlashVSR weights dir")
    parser.add_argument("--clip-start", type=float, default=None, help="Optional subclip start time in seconds")
    parser.add_argument("--clip-duration", type=float, default=None, help="Optional subclip duration in seconds")
    parser.add_argument("--max-render-frames", type=_parse_csv_ints, default=[21], help="Comma-separated list, e.g. 21,29")
    parser.add_argument("--tiny-tail-threshold", type=int, default=8)
    parser.add_argument("--tail-merge-min-render-frames", type=int, default=None)
    parser.add_argument("--num-inference-steps", type=_parse_csv_ints, default=[1], help="Comma-separated list, e.g. 1,2")
    parser.add_argument("--output-quality", type=_parse_csv_ints, default=[5], help="Comma-separated list, e.g. 5,8")
    parser.add_argument("--local-range", type=_parse_csv_ints, default=[11], help="Comma-separated list, e.g. 11,15")
    parser.add_argument("--kv-ratio", type=_parse_csv_floats, default=[3.0], help="Comma-separated list, e.g. 3.0,4.0")
    parser.add_argument("--cfg-scale", type=float, default=1.0)
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--topk-ratio-multiplier", type=float, default=2.0)
    parser.add_argument("--no-color-fix", action="store_true")
    args = parser.parse_args()

    output_root = Path(args.output_root).expanduser().resolve()
    output_root.mkdir(parents=True, exist_ok=True)

    source_path = Path(args.input).expanduser().resolve()
    sample_path = source_path
    if args.clip_start is not None and args.clip_duration is not None:
        sample_path = _extract_subclip(
            source_path,
            output_root / f"sample_{args.clip_start:.3f}s_{args.clip_duration:.3f}s.mp4",
            start=args.clip_start,
            duration=args.clip_duration,
        )

    sample_meta = probe_video(sample_path)
    summary = {
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "source_path": str(source_path),
        "sample_path": str(sample_path),
        "sample_video": asdict(sample_meta),
        "cases": [],
    }
    summary_path = output_root / "summary.json"
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

    for case_name, planner, render in _build_cases(args):
        case_root = output_root / case_name
        manifest_path = default_manifest_path(case_root)
        output_path = case_root / f"{sample_path.stem}_x{args.scale:g}.mp4"

        plan_video_job(
            input_path=sample_path,
            output_path=output_path,
            work_dir=case_root,
            scale=args.scale,
            planner=planner,
            manifest_path=manifest_path,
            upstream_root=args.upstream_root,
            infer_script=args.infer_script,
            weights_dir=args.weights_dir,
        )

        started = time.perf_counter()
        status = "succeeded"
        error = None
        try:
            run_manifest(
                manifest_path,
                upstream_root=args.upstream_root,
                infer_script=args.infer_script,
                weights_dir=args.weights_dir,
                render_config=render,
            )
        except Exception as exc:
            status = "failed"
            error = f"{type(exc).__name__}: {exc}"

        case_entry = {
            "case_name": case_name,
            "planner": asdict(planner),
            "render": asdict(render),
            "status": status,
            "error": error,
            "elapsed_seconds": round(time.perf_counter() - started, 2),
            "manifest_path": str(manifest_path),
            "output_path": str(output_path),
            "output_exists": output_path.exists(),
            "output_size_bytes": output_path.stat().st_size if output_path.exists() else None,
        }
        summary["cases"].append(case_entry)
        summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(case_entry, ensure_ascii=False))

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
