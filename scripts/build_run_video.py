#!/usr/bin/env python3
"""Build an MP4 replay from the screenshots saved by a completed MACU run."""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import tempfile
from pathlib import Path


def _step(path: Path) -> tuple[int, str]:
    try:
        return int(path.stem.rsplit("_", 1)[-1]), path.name
    except ValueError:
        return 10**9, path.name


def collect_frames(task_dir: Path) -> list[Path]:
    frames: list[Path] = []
    initial = task_dir / "initial_screenshot.png"
    if initial.is_file():
        frames.append(initial)

    graph_path = task_dir / "dependency_graph.json"
    graph = json.loads(graph_path.read_text()) if graph_path.is_file() else {}
    subtasks = ((graph.get("dependency_graph") or {}).get("subtasks") or [])
    order = [str(item.get("id")) for item in subtasks if item.get("id")]
    discovered = sorted(
        p.name for p in task_dir.iterdir()
        if p.is_dir() and (p / "results" / "pyautogui" / "screenshot").is_dir()
    )
    order.extend(name for name in discovered if name not in order)

    for subtask in order:
        base = task_dir / subtask / "results" / "pyautogui" / "screenshot"
        if not base.is_dir():
            continue
        shots = sorted(base.rglob("step_*.png"), key=lambda p: (_step(p), str(p)))
        frames.extend(shots)
    return frames


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("task_dir", type=Path)
    parser.add_argument("--seconds-per-frame", type=float, default=1.5)
    args = parser.parse_args()
    task_dir = args.task_dir.resolve()
    frames = collect_frames(task_dir)
    if not frames:
        parser.error(f"No screenshots found in {task_dir}")
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        parser.error("ffmpeg is required to build MP4 replay videos")

    output_dir = task_dir / "visualization"
    output_dir.mkdir(parents=True, exist_ok=True)
    output = output_dir / "replay.mp4"
    with tempfile.TemporaryDirectory(prefix="macu-video-") as temp:
        concat = Path(temp) / "frames.txt"
        lines: list[str] = []
        for frame in frames:
            escaped = str(frame).replace("'", "'\\''")
            lines.extend([f"file '{escaped}'", f"duration {args.seconds_per_frame}"])
        escaped = str(frames[-1]).replace("'", "'\\''")
        lines.append(f"file '{escaped}'")
        concat.write_text("\n".join(lines) + "\n")
        command = [
            ffmpeg, "-y", "-loglevel", "error", "-f", "concat", "-safe", "0",
            "-i", str(concat), "-vf",
            "fps=10,scale=1280:720:force_original_aspect_ratio=decrease,pad=1280:720:(ow-iw)/2:(oh-ih)/2",
            "-c:v", "libx264", "-preset", "fast", "-crf", "24", "-pix_fmt", "yuv420p",
            "-movflags", "+faststart", str(output),
        ]
        subprocess.run(command, check=True)
    print(f"Wrote {len(frames)}-frame video to {output}")


if __name__ == "__main__":
    main()
