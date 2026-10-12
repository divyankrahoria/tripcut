"""Turn editor masters (e.g. Palmier ProRes exports) into the final delivery MP4.

- Video: 10-bit H.265 (Main 10, tagged hvc1 so Apple players accept it), matching Insta360 camera quality.
  `hardware` uses Apple's VideoToolbox encoder (about a minute for 4K); `x265` is the slow software encoder.
- Audio: gentle compressor -> makeup gain -> limiter, solved so the whole film lands on the target loudness
  (-14 LUFS is what YouTube/Instagram normalise to; they turn louder videos down, never quiet ones up).
- Several masters (a film exported in parts to save disk space) are joined without re-encoding: video
  streams are concatenated as-is, audio is joined sample-exact from PCM, each part cut to its video length.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable
from pathlib import Path

from .ffmpeg import FFmpegError, run

SAMPLE_RATE = 48000
COMPRESSOR = "acompressor=threshold=-24dB:ratio=2.5:attack=15:release=250:knee=4"
LIMITER = "alimiter=limit=0.84:attack=3:release=60:level=disabled"  # 0.84 ~= -1.5 dBFS

_HARDWARE = ["-c:v", "hevc_videotoolbox", "-profile:v", "main10", "-pix_fmt", "p010le"]
_HARDWARE += ["-b:v", "80M", "-maxrate", "110M", "-bufsize", "160M"]
_X265 = ["-c:v", "libx265", "-preset", "medium", "-crf", "16", "-pix_fmt", "yuv420p10le", "-profile:v", "main10"]
_X265 += ["-x265-params", "log-level=error:colorprim=bt709:transfer=bt709:colormatrix=bt709"]
_BT709 = ["-color_primaries", "bt709", "-color_trc", "bt709", "-colorspace", "bt709"]


def has_hardware_hevc() -> bool:
    if sys.platform != "darwin":
        return False
    out = subprocess.run(["ffmpeg", "-hide_banner", "-encoders"], capture_output=True, text=True).stdout
    return "hevc_videotoolbox" in out


def master_chain(gain_db: float) -> str:
    return f"{COMPRESSOR},volume={gain_db:.2f}dB,{LIMITER}"


def measure_loudness(src: Path, chain: str = "anull") -> tuple[float, float]:
    """(integrated LUFS, true peak dBFS) of `src`'s audio after `chain`."""
    err = subprocess.run(
        ["ffmpeg", "-hide_banner", "-nostats", "-i", str(src), "-vn", "-af", f"{chain},ebur128=peak=true"]
        + ["-f", "null", "-"],
        capture_output=True,
        text=True,
    ).stderr
    summary = err[err.rfind("Summary:") :]
    lufs, peak = re.search(r"I:\s+(-?[\d.]+)", summary), re.search(r"Peak:\s+(-?[\d.]+)", summary)
    if not lufs or not peak:
        raise FFmpegError(f"could not measure loudness of {src.name}")
    return float(lufs.group(1)), float(peak.group(1))


def solve_gain(src: Path, target: float) -> float:
    """Makeup gain that puts the compressed + limited audio on `target` LUFS (the limiter makes it nonlinear)."""
    gain = 0.0
    for _ in range(4):
        lufs, _ = measure_loudness(src, master_chain(gain))
        if abs(lufs - target) < 0.3:
            break
        gain += target - lufs
    return gain


def _frames_and_fps(video: Path) -> tuple[int, float]:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=nb_frames,r_frame_rate"]
        + ["-of", "csv=p=0", str(video)],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    rate, frames = out.split(",")[:2]
    num, den = rate.split("/")
    return int(frames), int(num) / int(den)


def black_fraction(video: Path, threshold: float = 0.08) -> float:
    """Share of once-a-second samples whose average brightness is below `threshold` (0-1)."""
    # YAVG is on the 8-bit video scale, where 16 is black and 235 is white.
    err = subprocess.run(
        ["ffmpeg", "-hide_banner", "-nostats", "-i", str(video), "-an"]
        + ["-vf", "fps=1,scale=320:-2,format=yuv420p,signalstats,metadata=print:key=lavfi.signalstats.YAVG"]
        + ["-f", "null", "-"],
        capture_output=True,
        text=True,
    ).stderr
    levels = [float(v) for v in re.findall(r"YAVG=([\d.]+)", err)]
    if not levels:
        return 0.0
    return sum((v - 16) / 219 < threshold for v in levels) / len(levels)


def wait_for_master(path: Path, timeout: float = 3600, poll: float = 5) -> None:
    """Block until an editor has finished writing `path` (a .mov/.mp4 is only probe-able once complete)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            probe = subprocess.run(
                ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
                capture_output=True,
                text=True,
            )
            if probe.returncode == 0 and probe.stdout.strip():
                return
        time.sleep(poll)
    raise TimeoutError(f"{path.name} did not appear within {timeout / 60:.0f} minutes")


def finish(
    masters: list[Path],
    out: Path,
    *,
    encoder: str = "auto",
    target_lufs: float = -14.0,
    delete_masters: bool = False,
    wait: bool = False,
    tmp_dir: Path | None = None,
    on_step: Callable[[str], None] = lambda _msg: None,
) -> tuple[float, float]:
    """Encode `masters` (in order) into one delivery file at `out`. Returns its (LUFS, true peak).

    Masters are deleted (with `delete_masters`) only after `out` is fully written, so a failure never
    loses them. Working files go in `tmp_dir` (default: next to `out`).
    """
    if encoder == "auto":
        encoder = "hardware" if has_hardware_hevc() else "x265"
    video_args = {"hardware": _HARDWARE, "x265": _X265}[encoder]
    tmp_root = tmp_dir or out.parent

    with tempfile.TemporaryDirectory(prefix="tripcut-finish-", dir=tmp_root) as tmp_name:
        tmp = Path(tmp_name)
        parts: list[tuple[Path, Path, int]] = []
        for i, master in enumerate(masters):
            if wait:
                on_step(f"Waiting for {master.name}")
                wait_for_master(master)
            if i == 0:
                check_space(masters if not wait else [master], tmp_root, out, len(masters))
            on_step(f"Encoding {master.name} ({encoder})")
            video, audio = tmp / f"part{i}.mp4", tmp / f"part{i}.wav"
            run(["-i", str(master), "-map", "0:v:0", "-an", *video_args, "-tag:v", "hvc1", *_BT709, str(video)])
            frames, fps = _frames_and_fps(video)
            # An editor that loses access to its clips (e.g. a camera drive remounted) exports black picture
            # under working titles and music. Catch it before the master is deleted.
            if black_fraction(video) > 0.8:
                raise FFmpegError(
                    f"{master.name} is almost entirely black: the editor probably couldn't read the clips. "
                    "Check the preview, reopen the project, and export again."
                )
            # Pad or cut each part's audio to exactly its picture length so later parts stay in sync.
            samples = round(frames * SAMPLE_RATE / fps)
            fit = f"aresample={SAMPLE_RATE},apad,atrim=end_sample={samples}"
            run(["-i", str(master), "-map", "0:a:0", "-af", fit, "-c:a", "pcm_f32le", str(audio)])
            parts.append((video, audio, frames))

        if len(parts) == 1:
            joined_video, joined_audio = parts[0][0], parts[0][1]  # nothing to join; saves a full copy
            on_step("Mastering the sound")
        else:
            on_step("Joining and mastering the sound")
            listing = tmp / "parts.txt"
            listing.write_text("".join(f"file '{v.name}'\n" for v, _, _ in parts))
            joined_video, joined_audio = tmp / "video.mp4", tmp / "audio.wav"
            run(["-f", "concat", "-safe", "0", "-i", str(listing), "-c", "copy", str(joined_video)], cwd=tmp)
            for v, _, _ in parts:
                v.unlink()  # the joined copy replaces them
            inputs = [arg for _, a, _ in parts for arg in ("-i", str(a))]
            concat = "".join(f"[{i}:a]" for i in range(len(parts))) + f"concat=n={len(parts)}:v=0:a=1[a]"
            run([*inputs, "-filter_complex", concat, "-map", "[a]", "-c:a", "pcm_f32le", str(joined_audio)])

        gain = solve_gain(joined_audio, target_lufs)
        staged = tmp / out.name
        run(
            ["-i", str(joined_video), "-i", str(joined_audio), "-map", "0:v:0", "-map", "1:a:0", "-c:v", "copy"]
            + ["-af", master_chain(gain), "-c:a", "aac", "-b:a", "256k", "-movflags", "+faststart", str(staged)]
        )
        shutil.move(staged, out)
    result = measure_loudness(out)
    if delete_masters:
        for master in masters:
            master.unlink(missing_ok=True)
    return result


def _duration(path: Path) -> float:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
        capture_output=True,
        text=True,
    ).stdout.strip()
    return float(out) if out else 0.0


def check_space(masters: list[Path], tmp_root: Path, out: Path, n_parts: int = 1) -> None:
    """Fail early, before hours of work, if the working or output drive can't hold the result.

    The encode is capped at 110 Mbps; working files need the encoded picture twice (encoded + final)
    and three times when parts are joined, plus the uncompressed audio.
    """
    seconds = sum(_duration(m) for m in masters)
    if n_parts > len(masters):  # waiting for later parts: assume they're about as long as the first
        seconds *= n_parts / len(masters)
    video = seconds * 110e6 / 8
    need_tmp = video * (3 if n_parts > 1 else 2) + seconds * SAMPLE_RATE * 2 * 4 * 2
    free_tmp = shutil.disk_usage(tmp_root).free
    if free_tmp < need_tmp:
        raise FFmpegError(
            f"Not enough space for working files on {tmp_root}: needs about {need_tmp / 1e9:.1f} GB, "
            f"{free_tmp / 1e9:.1f} GB free. Free some space or pass --tmp-dir on a bigger drive."
        )
    if shutil.disk_usage(out.parent).free < video and os.stat(tmp_root).st_dev != os.stat(out.parent).st_dev:
        raise FFmpegError(f"Not enough space for the {video / 1e9:.1f} GB result in {out.parent}.")
