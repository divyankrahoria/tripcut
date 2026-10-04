"""Small wrappers around the ffmpeg CLI."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path


class FFmpegError(RuntimeError):
    pass


def run(args: list[str], *, quiet: bool = True, cwd: Path | None = None) -> subprocess.CompletedProcess:
    cmd = ["ffmpeg", "-hide_banner", "-y"] + (["-loglevel", "error"] if quiet else []) + args
    proc = subprocess.run(cmd, capture_output=True, text=True, cwd=cwd)
    if proc.returncode != 0:
        tail = "\n".join(proc.stderr.strip().splitlines()[-15:])
        raise FFmpegError(f"ffmpeg failed:\n  {' '.join(cmd)}\n{tail}")
    return proc


def make_proxy(src: Path, dst: Path, height: int = 480) -> Path:
    """Small, fast-to-upload copy with identical timing, for AI analysis."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    run(
        [
            "-i",
            str(src),
            "-vf",
            f"scale=-2:'min({height},ih)'",
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            "30",
            "-c:a",
            "aac",
            "-b:a",
            "64k",
            "-ac",
            "1",
            "-movflags",
            "+faststart",
            str(dst),
        ]
    )
    return dst


_SILENCE_START = re.compile(r"silence_start:\s*(-?[\d.]+)")
_SILENCE_END = re.compile(r"silence_end:\s*(-?[\d.]+)")


def loud_intervals(
    src: Path, duration: float, noise_db: float = -30.0, min_silence: float = 0.6
) -> list[tuple[float, float]]:
    """Intervals where the audio is above `noise_db` (i.e. probably someone talking or something happening)."""
    proc = subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-nostats",
            "-i",
            str(src),
            "-vn",
            "-af",
            f"silencedetect=noise={noise_db}dB:d={min_silence}",
            "-f",
            "null",
            "-",
        ],
        capture_output=True,
        text=True,
    )
    silences: list[tuple[float, float]] = []
    start: float | None = None
    for line in proc.stderr.splitlines():
        if m := _SILENCE_START.search(line):
            start = max(0.0, float(m.group(1)))
        elif (m := _SILENCE_END.search(line)) and start is not None:
            silences.append((start, float(m.group(1))))
            start = None
    if start is not None:
        silences.append((start, duration))

    loud, cursor = [], 0.0
    for s, e in silences:
        if s > cursor:
            loud.append((cursor, s))
        cursor = max(cursor, e)
    if cursor < duration:
        loud.append((cursor, duration))
    return [(round(a, 2), round(b, 2)) for a, b in loud if b - a > 0.05]
