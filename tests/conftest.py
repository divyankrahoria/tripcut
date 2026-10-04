import shutil
import subprocess
from pathlib import Path

import pytest

HAS_FFMPEG = shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None
needs_ffmpeg = pytest.mark.skipif(not HAS_FFMPEG, reason="ffmpeg not installed")


def _ff(*args: str) -> None:
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *args], check=True)


def make_talking_clip(path: Path, seconds: int = 14, when: str = "2026-09-12T10:15:30Z") -> Path:
    """Test pattern + a tone that switches on/off like someone talking in sentences."""
    _ff(
        "-f",
        "lavfi",
        "-i",
        f"testsrc2=size=640x360:rate=30:duration={seconds}",
        "-f",
        "lavfi",
        "-i",
        f"sine=frequency=220:duration={seconds}",
        "-filter_complex",
        "[1:a]volume='if(lt(mod(t,5),3.5),1,0)':eval=frame[a]",
        "-map",
        "0:v",
        "-map",
        "[a]",
        "-c:v",
        "libx264",
        "-preset",
        "ultrafast",
        "-c:a",
        "aac",
        "-metadata",
        f"creation_time={when}",
        str(path),
    )
    return path


def make_broll_clip(
    path: Path, seconds: int = 10, when: str | None = None, audio: bool = True, size: str = "640x360"
) -> Path:
    args = ["-f", "lavfi", "-i", f"mandelbrot=size={size}:rate=30", "-t", str(seconds)]
    if audio:
        args += ["-f", "lavfi", "-i", f"anoisesrc=amplitude=0.001:duration={seconds}", "-c:a", "aac"]
    args += ["-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p"]
    if when:
        args += ["-metadata", f"creation_time={when}"]
    _ff(*args, "-shortest" if audio else "-an", str(path))
    return path


@pytest.fixture
def trip(tmp_path: Path) -> Path:
    """A fake trip folder: two days, talking + b-roll, one silent clip, one unreframed 360 export."""
    folder = tmp_path / "lisbon"
    folder.mkdir()
    make_broll_clip(folder / "a_harbour.mp4", 10, "2026-09-12T09:00:00Z")
    make_talking_clip(folder / "b_intro.mp4", 14, "2026-09-12T10:15:30Z")
    make_broll_clip(folder / "VID_20260913_181500_00_007.mp4", 8, None, audio=False)  # date from filename
    make_broll_clip(folder / "c_tram.mp4", 9, "2026-09-13T12:00:00Z")
    make_broll_clip(folder / "raw360.mp4", 3, "2026-09-13T13:00:00Z", size="2880x1440")
    return folder
