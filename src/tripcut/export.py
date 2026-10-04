"""Side outputs: storyboard, subtitles, and an EDL timeline for DaVinci Resolve / Premiere."""

from __future__ import annotations

from pathlib import Path

from .models import ClipAnalysis, EditPlan, Segment


def timeline(plan: EditPlan) -> list[tuple[Segment, float]]:
    """Each segment with its start time in the finished video."""
    out, t = [], 0.0
    for s in plan.segments:
        out.append((s, t))
        t += s.duration
    return out


def fmt_clock(seconds: float) -> str:
    m, s = divmod(max(0.0, seconds), 60)
    return f"{int(m)}:{s:04.1f}"


# --------------------------------------------------------------------------- storyboard


def storyboard_md(plan: EditPlan, analyses: dict[str, ClipAnalysis] | None = None) -> str:
    lines = [
        f"# {plan.title}",
        "",
        f"**{len(plan.segments)} shots · {fmt_clock(plan.duration)} · {plan.aspect}**",
        "",
    ]
    if plan.notes:
        lines += [f"> {plan.notes}", ""]
    lines += ["| # | at | clip | in → out | role | caption | why |", "|---|---|---|---|---|---|---|"]
    for i, (s, t) in enumerate(timeline(plan), 1):
        why = (s.reason or "").replace("|", "/")
        lines.append(
            f"| {i} | {fmt_clock(t)} | `{s.clip}` | {s.start:.1f} → {s.end:.1f} | {s.role} | {s.caption or ''} | {why} |"
        )
    lines += [
        "",
        "_Edit `plan.json` (reorder, delete, change in/out times) and run `tripcut render` to re-cut._",
        "",
    ]
    return "\n".join(lines)


# --------------------------------------------------------------------------- subtitles


def transcript_on_timeline(plan: EditPlan, analyses: dict[str, ClipAnalysis]) -> list[tuple[float, float, str]]:
    """Map each clip's transcript lines into finished-video time, for kept speech only."""
    cues = []
    for s, t0 in timeline(plan):
        a = analyses.get(s.clip)
        if not a:
            continue
        for line in a.transcript:
            a_start, a_end = max(line.start, s.start), min(line.end, s.end)
            if a_end - a_start < 0.3 or not line.text:
                continue
            cues.append((t0 + a_start - s.start, t0 + a_end - s.start, line.text))
    return cues


def _srt_ts(t: float) -> str:
    ms = int(round(t * 1000))
    h, ms = divmod(ms, 3_600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    return f"{h:02}:{m:02}:{s:02},{ms:03}"


def to_srt(cues: list[tuple[float, float, str]]) -> str:
    return "\n".join(f"{i}\n{_srt_ts(a)} --> {_srt_ts(b)}\n{text}\n" for i, (a, b, text) in enumerate(cues, 1))


def _ass_ts(t: float) -> str:
    cs = int(round(max(0.0, t) * 100))
    h, cs = divmod(cs, 360_000)
    m, cs = divmod(cs, 6000)
    s, cs = divmod(cs, 100)
    return f"{h}:{m:02}:{s:02}.{cs:02}"


def _ass_escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace("{", "(").replace("}", ")").replace("\n", "\\N")


def to_ass(plan: EditPlan, width: int, height: int, cues: list[tuple[float, float, str]] | None = None) -> str:
    """Styled captions: centred title on the opener, lower-third place names, optional subtitles."""
    base = min(width, height)
    title_size, cap_size, sub_size = int(base * 0.085), int(base * 0.05), int(base * 0.045)
    margin = int(base * 0.06)
    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {width}
PlayResY: {height}
WrapStyle: 0
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Title,Helvetica,{title_size},&H00FFFFFF,&H00FFFFFF,&H64000000,&H00000000,-1,0,0,0,100,100,2,0,1,0,3,5,{margin},{margin},{margin},1
Style: Place,Helvetica,{cap_size},&H00FFFFFF,&H00FFFFFF,&H64000000,&H00000000,-1,0,0,0,100,100,1,0,1,0,2,1,{margin},{margin},{margin},1
Style: Sub,Helvetica,{sub_size},&H00FFFFFF,&H00FFFFFF,&H00000000,&H96000000,0,0,0,0,100,100,0,0,3,2,0,2,{margin},{margin},{int(margin * 1.6)},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    events = []
    for i, (s, t0) in enumerate(timeline(plan)):
        if not s.caption:
            continue
        style = "Title" if i == 0 else "Place"
        end = t0 + min(3.5, s.duration)
        events.append(
            f"Dialogue: 1,{_ass_ts(t0 + 0.2)},{_ass_ts(end)},{style},,0,0,0,,{{\\fad(350,350)}}{_ass_escape(s.caption)}"
        )
    for a, b, text in cues or []:
        events.append(f"Dialogue: 0,{_ass_ts(a)},{_ass_ts(b)},Sub,,0,0,0,,{_ass_escape(text)}")
    return header + "\n".join(events) + "\n"


# --------------------------------------------------------------------------- EDL


def _tc(seconds: float, fps: int) -> str:
    frames = int(round(seconds * fps))
    h, frames = divmod(frames, 3600 * fps)
    m, frames = divmod(frames, 60 * fps)
    s, f = divmod(frames, fps)
    return f"{h:02}:{m:02}:{s:02}:{f:02}"


def to_edl(plan: EditPlan, fps: int = 30) -> str:
    """CMX 3600 EDL. In Resolve: import your clips into the Media Pool, then
    File > Import > Timeline and pick this file - it links shots by clip name."""
    lines = [f"TITLE: {plan.title[:60]}", "FCM: NON-DROP FRAME", ""]
    rec = 3600.0  # timelines conventionally start at 01:00:00:00
    for i, s in enumerate(plan.segments, 1):
        reel = "AX"
        src_in, src_out = _tc(s.start, fps), _tc(s.end, fps)
        rec_in, rec_out = _tc(rec, fps), _tc(rec + s.duration, fps)
        lines.append(f"{i:03}  {reel:<8} AA/V  C        {src_in} {src_out} {rec_in} {rec_out}")
        lines.append(f"* FROM CLIP NAME: {s.clip}")
        if s.caption:
            lines.append(f"* COMMENT: {s.caption}")
        lines.append("")
        rec += s.duration
    return "\n".join(lines)


def write_all(plan: EditPlan, analyses: dict[str, ClipAnalysis], outdir: Path, fps: int = 30) -> dict[str, Path]:
    outdir.mkdir(parents=True, exist_ok=True)
    files = {
        "storyboard": outdir / "storyboard.md",
        "edl": outdir / "timeline.edl",
    }
    files["storyboard"].write_text(storyboard_md(plan, analyses))
    files["edl"].write_text(to_edl(plan, fps))
    cues = transcript_on_timeline(plan, analyses)
    if cues:
        files["srt"] = outdir / "subtitles.srt"
        files["srt"].write_text(to_srt(cues))
    return files
