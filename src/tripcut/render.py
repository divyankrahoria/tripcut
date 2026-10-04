"""Stage 4: turn an EditPlan into an MP4 with FFmpeg."""

from __future__ import annotations

import shutil
from collections.abc import Callable
from pathlib import Path

from . import ffmpeg
from .export import timeline, to_ass, transcript_on_timeline
from .models import ClipAnalysis, ClipInfo, EditPlan

ASPECTS = {"16:9": (1920, 1080), "9:16": (1080, 1920), "1:1": (1080, 1080), "4:5": (1080, 1350)}


def output_size(aspect: str, height_hint: int = 1080) -> tuple[int, int]:
    w, h = ASPECTS.get(aspect, ASPECTS["16:9"])
    scale = height_hint / min(w, h)
    return int(round(w * scale / 2) * 2), int(round(h * scale / 2) * 2)


def render(
    plan: EditPlan,
    clips: dict[str, ClipInfo],
    workdir: Path,
    out_path: Path,
    *,
    analyses: dict[str, ClipAnalysis] | None = None,
    fps: int = 30,
    quality: int = 1080,
    music: Path | None = None,
    captions: bool = True,
    subtitles: bool = False,
    progress: Callable[[int, int], None] | None = None,
) -> Path:
    if not plan.segments:
        raise ValueError("Nothing to render: the plan has no segments.")
    W, H = output_size(plan.aspect, quality)
    segdir = workdir / "render"
    if segdir.exists():
        shutil.rmtree(segdir)
    segdir.mkdir(parents=True)

    natural_vol = 0.35 if music else 0.8  # ambient sound under b-roll
    n = len(plan.segments)
    parts: list[Path] = []

    for i, seg in enumerate(plan.segments):
        clip = clips[seg.clip]
        dur = round(seg.duration, 3)
        part = segdir / f"{i:04}.mp4"

        vf = [
            f"scale={W}:{H}:force_original_aspect_ratio=increase",
            f"crop={W}:{H}",
            "setsar=1",
            f"fps={fps}",
            "format=yuv420p",
        ]
        if i == 0:
            vf.append("fade=t=in:st=0:d=0.6")
        if i == n - 1:
            vf.append(f"fade=t=out:st={max(0, dur - 1.0):.3f}:d=1.0")

        vol = 1.0 if seg.role == "speech" else natural_vol
        af = [
            "aresample=48000",
            "aformat=sample_fmts=fltp:channel_layouts=stereo",
            f"volume={vol}",
            "afade=t=in:st=0:d=0.04",  # micro-fades kill clicks at hard cuts
            f"afade=t=out:st={max(0, dur - 0.04):.3f}:d=0.04",
        ]

        args = ["-ss", f"{seg.start:.3f}", "-t", f"{dur:.3f}", "-i", clip.path]
        if clip.has_audio:
            audio_map = "0:a:0"
        else:
            args += ["-f", "lavfi", "-t", f"{dur:.3f}", "-i", "anullsrc=r=48000:cl=stereo"]
            audio_map = "1:a:0"
        args += [
            "-map",
            "0:v:0",
            "-map",
            audio_map,
            "-vf",
            ",".join(vf),
            "-af",
            ",".join(af),
            "-c:v",
            "libx264",
            "-preset",
            "medium",
            "-crf",
            "19",
            "-c:a",
            "aac",
            "-b:a",
            "192k",
            "-t",
            f"{dur:.3f}",
            "-video_track_timescale",
            "90000",
            str(part),
        ]
        ffmpeg.run(args)
        parts.append(part)
        if progress:
            progress(i + 1, n)

    listfile = segdir / "concat.txt"
    listfile.write_text("".join(f"file '{p.name}'\n" for p in parts))
    joined = segdir / "joined.mp4"
    ffmpeg.run(["-f", "concat", "-safe", "0", "-i", str(listfile), "-c", "copy", str(joined)])

    total = plan.duration
    cues = transcript_on_timeline(plan, analyses or {}) if subtitles else []
    want_captions = (captions and any(s.caption for s in plan.segments)) or bool(cues)

    if not music and not want_captions:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(joined), out_path)
        return out_path

    args = ["-i", joined.name]
    filters: list[str] = []
    vmap, amap = "0:v", "0:a"

    if want_captions:
        ass = segdir / "captions.ass"
        ass.write_text(to_ass(plan if captions else _strip_captions(plan), W, H, cues))
        filters.append("[0:v]subtitles=captions.ass[v]")
        vmap = "[v]"

    if music:
        args = ["-i", joined.name, "-stream_loop", "-1", "-i", str(music.resolve())]
        speech = [(t, t + s.duration) for s, t in timeline(plan) if s.role == "speech"]
        duck = "+".join(f"between(t,{a:.2f},{b:.2f})" for a, b in speech) or "0"
        filters.append(
            "[1:a]aresample=48000,aformat=sample_fmts=fltp:channel_layouts=stereo,"
            f"atrim=0:{total:.3f},asetpts=PTS-STARTPTS,"
            f"volume='if({duck},0.12,0.55)':eval=frame,"
            f"afade=t=in:st=0:d=1.5,afade=t=out:st={max(0, total - 2.5):.3f}:d=2.5[m];"
            "[0:a][m]amix=inputs=2:duration=first:normalize=0[a]"
        )
        amap = "[a]"

    args += ["-filter_complex", ";".join(filters), "-map", vmap, "-map", amap]
    args += ["-c:v", "libx264", "-preset", "medium", "-crf", "19"] if want_captions else ["-c:v", "copy"]
    args += ["-c:a", "aac", "-b:a", "192k"] if music else ["-c:a", "copy"]
    args += ["-movflags", "+faststart", "final.mp4"]
    ffmpeg.run(args, cwd=segdir)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(segdir / "final.mp4"), out_path)
    return out_path


def _strip_captions(plan: EditPlan) -> EditPlan:
    return EditPlan(
        plan.title,
        [type(s)(s.clip, s.start, s.end, s.role, None, s.reason) for s in plan.segments],
        plan.aspect,
        plan.notes,
    )
