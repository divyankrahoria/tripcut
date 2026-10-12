"""Stage 3: decide the story - which moments, in what order, for how long."""

from __future__ import annotations

from datetime import datetime

from .analyze import to_seconds
from .models import ClipAnalysis, ClipInfo, EditPlan, Moment, Segment
from .prompts import PLAN_PROMPT, clip_log

MIN_SEGMENT = 1.2  # seconds; anything shorter reads as a glitch
MAX_SPEECH = 45.0
MAX_BROLL = 6.0


# --------------------------------------------------------------------------- validation


def validate_plan(plan: EditPlan, clips: list[ClipInfo], target: float) -> tuple[EditPlan, list[str]]:
    """Make an AI- or human-written plan safe to render. Returns (plan, warnings)."""
    by_name = {c.name: c for c in clips}
    by_stem = {c.name.rsplit(".", 1)[0]: c for c in clips}
    warnings: list[str] = []
    fixed: list[Segment] = []

    for i, seg in enumerate(plan.segments):
        clip = by_name.get(seg.clip) or by_stem.get(seg.clip.rsplit(".", 1)[0])
        if clip is None:
            warnings.append(f"segment {i + 1}: unknown clip {seg.clip!r}, dropped")
            continue
        start = max(0.0, min(float(seg.start), clip.duration))
        end = max(0.0, min(float(seg.end), clip.duration))
        if end < start:
            start, end = end, start
        if end - start < MIN_SEGMENT:
            warnings.append(f"segment {i + 1}: {clip.name} {start:.1f}-{end:.1f}s is too short, dropped")
            continue
        if (start, end) != (seg.start, seg.end):
            warnings.append(f"segment {i + 1}: clamped times to fit {clip.name} ({clip.duration:.1f}s)")
        fixed.append(
            Segment(clip.name, round(start, 3), round(end, 3), seg.role or "broll", seg.caption or None, seg.reason)
        )

    # Back-to-back pieces of the same continuous shot read as a jump cut: join them.
    merged: list[Segment] = []
    for seg in fixed:
        prev = merged[-1] if merged else None
        if prev and prev.clip == seg.clip and 0 <= seg.start - prev.end < 0.15 and prev.role == seg.role:
            prev.end = seg.end
            prev.caption = prev.caption or seg.caption
        else:
            merged.append(seg)

    out = EditPlan(plan.title or "My trip", merged, plan.aspect, plan.notes)

    # If the planner overshot badly, tighten b-roll first (never chop speech mid-thought).
    if target and out.duration > target * 1.3:
        before = out.duration
        over = before - target * 1.15
        broll = [s for s in out.segments if s.role != "speech"]
        slack = sum(max(0.0, s.duration - 2.0) for s in broll)
        if slack > 0:
            ratio = min(1.0, over / slack)
            for s in broll:
                cut = max(0.0, s.duration - 2.0) * ratio
                s.start = round(s.start + cut / 2, 3)
                s.end = round(s.end - cut / 2, 3)
            warnings.append(f"plan was {before:.0f}s; trimmed b-roll to {out.duration:.0f}s (target {target:.0f}s)")

    if not out.segments:
        warnings.append("plan has no usable segments")
    return out, warnings


# --------------------------------------------------------------------------- heuristic planner


def heuristic_plan(
    clips: list[ClipInfo], analyses: dict[str, ClipAnalysis], target: float, aspect: str, title: str | None = None
) -> EditPlan:
    """Chronological, budgeted selection. No AI needed - used by `--provider local`
    and as a fallback if the AI planner fails."""
    candidates: list[tuple[ClipInfo, Moment]] = []
    for c in clips:
        a = analyses.get(c.name)
        if not a or c.is_360:
            continue
        for m in a.moments:
            candidates.append((c, m))
    if not candidates:
        return EditPlan(title or "My trip", [], aspect)

    def trimmed(m: Moment) -> tuple[float, float]:
        cap = MAX_SPEECH if m.kind == "speech" else min(MAX_BROLL, 4.0)
        if m.duration <= cap:
            return m.start, m.end
        if m.kind == "speech":
            return m.start, m.start + cap
        mid = (m.start + m.end) / 2
        return mid - cap / 2, mid + cap / 2

    # Pick by score until we hit the target, but guarantee every clip a shot if possible,
    # so the whole trip is represented.
    ranked = sorted(candidates, key=lambda cm: (-cm[1].score, cm[1].kind != "speech"))
    chosen: list[tuple[ClipInfo, Moment, float, float]] = []
    seen_clips: set[str] = set()
    total = 0.0

    best_per_clip = {}
    for c, m in ranked:
        best_per_clip.setdefault(c.name, (c, m))
    first_pass = sorted(best_per_clip.values(), key=lambda cm: -cm[1].score)
    for c, m in first_pass + [cm for cm in ranked if cm not in first_pass]:
        s, e = trimmed(m)
        if total + (e - s) > target * 1.1 and c.name in seen_clips:
            continue
        if total >= target * 1.05:
            break
        chosen.append((c, m, s, e))
        seen_clips.add(c.name)
        total += e - s

    order = {c.name: i for i, c in enumerate(clips)}
    chosen.sort(key=lambda x: (order[x[0].name], x[2]))

    segments: list[Segment] = []
    last_day: str | None = None
    for i, (c, m, s, e) in enumerate(chosen):
        caption = None
        day = _day_label(c.created_dt)
        if day and day != last_day:
            caption = day
            last_day = day
        role = "speech" if m.kind == "speech" else "broll"
        if i == 0:
            role = "opener" if role == "broll" else role
        segments.append(Segment(c.name, round(s, 3), round(e, 3), role, caption, m.description))

    if segments and segments[-1].role == "broll":
        segments[-1].role = "closer"
    return EditPlan(title or "My trip", segments, aspect, "chronological cut (heuristic planner)")


def _day_label(dt: datetime | None) -> str | None:
    return dt.strftime("%a %d %b") if dt else None


# --------------------------------------------------------------------------- AI planner


def ai_plan(
    provider,  # GeminiProvider (has .generate_json)
    clips: list[ClipInfo],
    analyses: dict[str, ClipAnalysis],
    target: float,
    aspect: str,
    style: str,
) -> EditPlan:
    entries = []
    for c in clips:
        a = analyses.get(c.name)
        if not a or c.is_360:
            continue
        d = a.to_dict()
        d.update(name=c.name, created=c.created_at, duration=c.duration, gps=c.location, orientation=c.orientation)
        entries.append(d)
    prompt = PLAN_PROMPT.format(
        target=target,
        lo=target * 0.85,
        hi=target * 1.15,
        aspect=aspect,
        style=style,
        clip_log=clip_log(entries),
    )
    raw = provider.generate_json([prompt])
    segments = []
    for s in raw.get("segments") or []:
        try:
            segments.append(
                Segment(
                    clip=str(s["clip"]),
                    start=to_seconds(s["start"]),
                    end=to_seconds(s["end"]),
                    role=str(s.get("role") or "broll"),
                    caption=s.get("caption") or None,
                    reason=str(s.get("reason") or ""),
                )
            )
        except (KeyError, TypeError, ValueError):
            continue
    return EditPlan(str(raw.get("title") or "My trip"), segments, aspect, str(raw.get("notes") or ""))
