"""Find clips in a folder and read what ffprobe knows about them."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from .models import ClipInfo

VIDEO_EXTS = {".mp4", ".mov", ".m4v", ".mkv", ".avi", ".webm"}
RAW_360_EXTS = {".insv", ".lrv"}  # Insta360 raw / low-res preview files

# Insta360 (and many phones) name files like VID_20260912_101530_00_012.mp4
_FILENAME_TS = re.compile(r"(20\d{2})(\d{2})(\d{2})[_-]?(\d{2})(\d{2})(\d{2})")


class ToolMissingError(RuntimeError):
    pass


def require_ffmpeg() -> None:
    for tool in ("ffmpeg", "ffprobe"):
        if shutil.which(tool) is None:
            raise ToolMissingError(
                f"`{tool}` not found on PATH. Install FFmpeg first: "
                "macOS `brew install ffmpeg`, Ubuntu `sudo apt install ffmpeg`, "
                "Windows `winget install ffmpeg`."
            )


def find_clips(folder: Path) -> tuple[list[Path], list[Path]]:
    """Return (usable video files, raw 360 files we can't use directly)."""
    usable, raw = [], []
    for p in sorted(folder.rglob("*")):
        if not p.is_file() or any(part.startswith(".") for part in p.relative_to(folder).parts):
            continue
        ext = p.suffix.lower()
        if ext in VIDEO_EXTS:
            usable.append(p)
        elif ext in RAW_360_EXTS:
            raw.append(p)
    return usable, raw


def _parse_rate(rate: str | None) -> float:
    if not rate or rate == "0/0":
        return 30.0
    if "/" in rate:
        num, den = rate.split("/", 1)
        try:
            return float(num) / float(den) if float(den) else 30.0
        except ValueError:
            return 30.0
    try:
        return float(rate)
    except ValueError:
        return 30.0


def timestamp_from_filename(name: str) -> datetime | None:
    m = _FILENAME_TS.search(name)
    if not m:
        return None
    try:
        return datetime(*(int(g) for g in m.groups()))
    except ValueError:
        return None


def _parse_iso(value: str) -> datetime | None:
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    # Cameras that don't know the date write 1970 or 1904; treat as unknown.
    if dt.year < 2005:
        return None
    return dt


def probe(path: Path) -> ClipInfo:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(path)],
        capture_output=True,
        text=True,
    )
    if out.returncode != 0:
        raise RuntimeError(f"ffprobe failed on {path.name}: {out.stderr.strip()}")
    data = json.loads(out.stdout or "{}")
    streams = data.get("streams", [])
    fmt = data.get("format", {})
    v = next((s for s in streams if s.get("codec_type") == "video"), None)
    if v is None:
        raise RuntimeError(f"{path.name} has no video stream")
    has_audio = any(s.get("codec_type") == "audio" for s in streams)

    width, height = int(v.get("width", 0)), int(v.get("height", 0))
    # Phones record rotation as metadata; swap so we know the real orientation.
    rotation = 0
    for sd in v.get("side_data_list", []) or []:
        if "rotation" in sd:
            rotation = abs(int(sd["rotation"]))
    if rotation in (90, 270):
        width, height = height, width

    duration = float(fmt.get("duration") or v.get("duration") or 0.0)

    tags = {k.lower(): val for k, val in (fmt.get("tags") or {}).items()}
    tags.update({k.lower(): val for k, val in (v.get("tags") or {}).items() if k.lower() not in tags})

    created, source = None, "unknown"
    if "creation_time" in tags and (dt := _parse_iso(tags["creation_time"])):
        created, source = dt, "metadata"
    elif dt := timestamp_from_filename(path.name):
        created, source = dt, "filename"
    else:
        created = datetime.fromtimestamp(os.path.getmtime(path), tz=timezone.utc)
        source = "mtime"

    location = tags.get("location") or tags.get("com.apple.quicktime.location.iso6709")

    # An equirectangular export is exactly 2:1. Reframed exports are 16:9 / 9:16 etc.
    is_360 = width >= 2880 and abs(width / max(height, 1) - 2.0) < 0.01

    return ClipInfo(
        path=str(path.resolve()),
        name=path.name,
        duration=round(duration, 3),
        width=width,
        height=height,
        fps=round(_parse_rate(v.get("avg_frame_rate") or v.get("r_frame_rate")), 3),
        has_audio=has_audio,
        created_at=created.isoformat() if created else None,
        created_source=source,
        location=location,
        is_360=is_360,
    )


def sort_chronologically(clips: list[ClipInfo]) -> list[ClipInfo]:
    def key(c: ClipInfo):
        dt = c.created_dt
        if dt is None:
            return (1, 0.0, c.name)
        # Compare naive and aware datetimes safely by dropping tz (same camera, same clock).
        return (0, dt.replace(tzinfo=None).timestamp(), c.name)

    return sorted(clips, key=key)
