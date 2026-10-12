"""tripcut command line."""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from pathlib import Path

import typer
from rich.console import Console
from rich.progress import BarColumn, MofNCompleteColumn, Progress, SpinnerColumn, TextColumn, TimeElapsedColumn
from rich.table import Table

from . import __version__
from .analyze import DEFAULT_MODEL, analyze_clip, make_provider
from .export import fmt_clock, write_all
from .ffmpeg import FFmpegError
from .finish import finish, has_hardware_hevc
from .models import ClipAnalysis, ClipInfo, EditPlan
from .plan import ai_plan, heuristic_plan, validate_plan
from .probe import ToolMissingError, find_clips, probe, require_ffmpeg, sort_chronologically
from .render import ASPECTS, render

app = typer.Typer(
    add_completion=False,
    rich_markup_mode="rich",
    help="[bold]tripcut[/] - AI watches your travel footage and cuts it into a story.",
    no_args_is_help=True,
)
console = Console()

FolderArg = typer.Argument(
    ...,
    file_okay=False,
    resolve_path=True,
    help="The trip's folder: its clips, or (with --source) just where tripcut keeps its notes.",
)
SourceOpt = typer.Option(
    None,
    "--source",
    "-S",
    file_okay=False,
    resolve_path=True,
    help="Read clips from here instead, e.g. the camera or SD card (repeatable). Remembered for later commands.",
)
FromOpt = typer.Option(None, "--from", help="Only clips recorded on or after this date (YYYY-MM-DD). Remembered.")
ToOpt = typer.Option(None, "--to", help="Only clips recorded on or before this date (YYYY-MM-DD). Remembered.")
ProviderOpt = typer.Option(
    "gemini", "--provider", "-p", help="[cyan]gemini[/] (best) or [cyan]local[/] (no AI, no upload)."
)
ModelOpt = typer.Option(DEFAULT_MODEL, "--model", help="Gemini model name.")
DurationOpt = typer.Option(90.0, "--duration", "-d", help="Target length of the final video, in seconds.")
AspectOpt = typer.Option("16:9", "--aspect", "-a", help=f"One of {', '.join(ASPECTS)}. 9:16 for Reels/Shorts.")
JobsOpt = typer.Option(4, "--jobs", "-j", help="Clips to analyse at once (Gemini paid tier handles 4+ easily).")
StyleOpt = typer.Option(
    "upbeat, cinematic travel vlog; warm and personal",
    "--style",
    "-s",
    help="Plain-English direction for the AI editor.",
)


def _workdir(folder: Path) -> Path:
    wd = folder / ".tripcut"
    wd.mkdir(exist_ok=True)
    return wd


def _default_out(folder: Path, aspect: str) -> Path:
    suffix = "" if aspect == "16:9" else "-" + aspect.replace(":", "x")
    return folder.parent / f"{folder.name}-tripcut{suffix}.mp4"


def _fail(msg: str) -> None:
    console.print(f"[bold red]✗[/] {msg}")
    raise typer.Exit(1)


def _parse_day(value: str | None, flag: str) -> date | None:
    if value is None:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        _fail(f"{flag} must be a date like 2026-06-30, not {value!r}")


def _sources(folder: Path, sources: list[Path] | None, since: str | None, until: str | None):
    """Where the clips are and which days belong to the trip. Given options are saved in .tripcut/trip.json
    so later commands (plan, render) find footage that lives on a camera or SD card without repeating them."""
    saved_file = folder / ".tripcut" / "trip.json"
    saved = json.loads(saved_file.read_text()) if saved_file.exists() else {}
    if sources or since or until:
        saved = {
            "sources": [str(s) for s in sources] if sources else saved.get("sources", []),
            "from": since or saved.get("from"),
            "to": until or saved.get("to"),
        }
        folder.mkdir(parents=True, exist_ok=True)
        _workdir(folder)
        saved_file.write_text(json.dumps(saved, indent=2))
    dirs = [Path(s) for s in saved.get("sources", [])] or [folder]
    for d in dirs:
        if not d.is_dir():
            _fail(f"Can't find {d}" + (" - is the drive plugged in?" if str(d).startswith("/Volumes/") else ""))
    return dirs, _parse_day(saved.get("from"), "--from"), _parse_day(saved.get("to"), "--to")


def _scan(
    folder: Path, sources: list[Path] | None = None, since: str | None = None, until: str | None = None
) -> list[ClipInfo]:
    try:
        require_ffmpeg()
    except ToolMissingError as e:
        _fail(str(e))
    dirs, first_day, last_day = _sources(folder, sources, since, until)
    files: list[Path] = []
    raw: list[Path] = []
    seen: set[str] = set()
    for d in dirs:
        found, found_raw = find_clips(d)
        raw += found_raw
        for f in found:
            if "-tripcut" in f.stem:
                continue
            if f.name in seen:  # clips are keyed by file name throughout (plan.json, analysis cache)
                console.print(f"[yellow]![/] Skipping {f} - another clip is already called {f.name}")
                continue
            seen.add(f.name)
            files.append(f)
    if raw:
        console.print(
            f"[yellow]![/] Skipping {len(raw)} raw Insta360 360° file(s) (.insv). "
            "Export them as reframed MP4s first (Insta360 Studio or app → AI Frame / Auto Frame)."
        )
    if not files:
        _fail(f"No video files found in {', '.join(map(str, dirs))}")

    clips: list[ClipInfo] = []
    with console.status(f"Reading {len(files)} clips…"):
        for f in files:
            try:
                clips.append(probe(f))
            except RuntimeError as e:
                console.print(f"[yellow]![/] {e}")
    if first_day or last_day:
        undated = [c for c in clips if c.created_dt is None]
        clips = [
            c
            for c in clips
            if c.created_dt is not None
            and (first_day is None or c.created_dt.date() >= first_day)
            and (last_day is None or c.created_dt.date() <= last_day)
        ]
        if undated:
            console.print(f"[yellow]![/] Skipping {len(undated)} clip(s) with no recording date to check --from/--to")
        if not clips:
            _fail(f"No clips recorded between {first_day or 'the start'} and {last_day or 'the end'}")
    clips = sort_chronologically(clips)

    flat360 = [c for c in clips if c.is_360]
    if flat360:
        console.print(
            f"[yellow]![/] {len(flat360)} clip(s) look like unreframed 360° (2:1) exports and will be skipped: "
            + ", ".join(c.name for c in flat360[:5])
            + ("…" if len(flat360) > 5 else "")
            + "\n  Reframe them in Insta360 Studio/app (AI Frame) and export as 16:9 or 9:16."
        )
    usable = [c for c in clips if not c.is_360]
    if not usable:
        _fail("No usable (non-360) clips.")

    total = sum(c.duration for c in usable)
    dated = sum(c.created_source != "mtime" for c in usable)
    console.print(
        f"[green]✓[/] {len(usable)} clips, {fmt_clock(total)} of footage "
        f"[dim]({dated}/{len(usable)} with a recorded date for ordering)[/]"
    )
    (_workdir(folder) / "clips.json").write_text(json.dumps([c.to_dict() for c in clips], indent=2))
    return usable


def _analyze(folder: Path, clips: list[ClipInfo], provider_name: str, model: str, force: bool, jobs: int = 4):
    try:
        provider = make_provider(provider_name, model)
    except RuntimeError as e:
        _fail(str(e))
    wd = _workdir(folder)
    analyses: dict[str, ClipAnalysis] = {}
    cached = 0
    with (
        Progress(
            SpinnerColumn(),
            TextColumn("[bold]{task.description}"),
            BarColumn(),
            MofNCompleteColumn(),
            TextColumn("[dim]{task.fields[clip]}"),
            TimeElapsedColumn(),
            console=console,
        ) as bar,
        # Most of each clip's time is spent waiting on Gemini, so a few at once is several times faster.
        ThreadPoolExecutor(max_workers=max(1, jobs)) as pool,
    ):
        task = bar.add_task(f"Watching clips ({provider.name})", total=len(clips), clip="")
        futures = {pool.submit(analyze_clip, provider, c, wd, force): c for c in clips}
        for done in as_completed(futures):
            c = futures[done]
            bar.update(task, clip=c.name)
            try:
                a, was_cached = done.result()
                analyses[c.name] = a
                cached += was_cached
            except Exception as e:  # keep going; one bad clip shouldn't sink the trip
                console.print(f"[yellow]![/] {c.name}: {e}")
            bar.advance(task)
    msg = f"[green]✓[/] Analysed {len(analyses)}/{len(clips)} clips"
    if cached:
        msg += f" [dim]({cached} from cache)[/]"
    console.print(msg)
    return provider, analyses


def _print_analyses(clips: list[ClipInfo], analyses: dict[str, ClipAnalysis]) -> None:
    t = Table(show_lines=False, header_style="bold", expand=True)
    t.add_column("clip", no_wrap=True)
    t.add_column("when", no_wrap=True, style="dim")
    t.add_column("len", justify="right")
    t.add_column("type")
    t.add_column("what's in it", ratio=1)
    t.add_column("best", justify="right")
    for c in clips:
        a = analyses.get(c.name)
        if not a:
            continue
        best = max((m.score for m in a.moments), default=0)
        when = c.created_dt.strftime("%b %d %H:%M") if c.created_dt else "?"
        t.add_row(c.name, when, f"{c.duration:.0f}s", a.scene_type, a.summary or "-", f"{best:g}")
    console.print(t)


def _plan(provider, provider_name, clips, analyses, duration, aspect, style, folder: Path) -> EditPlan:
    plan: EditPlan | None = None
    if provider_name == "gemini":
        with console.status("Planning the story…"):
            try:
                plan = ai_plan(provider, clips, analyses, duration, aspect, style)
            except Exception as e:
                console.print(f"[yellow]![/] AI planner failed ({e}); using the heuristic planner instead.")
    if plan is None or not plan.segments:
        plan = heuristic_plan(clips, analyses, duration, aspect, title=folder.name)

    plan, warnings = validate_plan(plan, clips, duration)
    for w in warnings:
        console.print(f"[dim]  · {w}[/]")
    if not plan.segments:
        _fail("The plan came back empty. Try --provider local, or check the analysis with `tripcut analyze`.")

    wd = _workdir(folder)
    (wd / "plan.json").write_text(json.dumps(plan.to_dict(), indent=2))
    files = write_all(plan, analyses, wd)
    console.print(f'[green]✓[/] Plan: [bold]"{plan.title}"[/] - {len(plan.segments)} shots, {fmt_clock(plan.duration)}')
    if plan.notes:
        console.print(f"  [dim italic]{plan.notes}[/]")
    console.print(f"  [dim]storyboard → {files['storyboard']}[/]")
    console.print(f"  [dim]timeline   → {files['edl']}  (import into DaVinci Resolve / Premiere)[/]")
    return plan


def _render(folder, plan, clips, analyses, out, music, captions, subtitles, quality) -> Path:
    clip_map = {c.name: c for c in clips}
    missing = [s.clip for s in plan.segments if s.clip not in clip_map]
    if missing:
        _fail(f"plan.json refers to clips that aren't in the folder: {', '.join(sorted(set(missing)))}")
    with Progress(
        SpinnerColumn(),
        TextColumn("[bold]Rendering"),
        BarColumn(),
        MofNCompleteColumn(),
        TimeElapsedColumn(),
        console=console,
    ) as bar:
        task = bar.add_task("render", total=len(plan.segments))
        out = render(
            plan,
            clip_map,
            _workdir(folder),
            out,
            analyses=analyses,
            quality=quality,
            music=music,
            captions=captions,
            subtitles=subtitles,
            progress=lambda done, _total: bar.update(task, completed=done),
        )
    console.print(f"[bold green]✓ Done![/] {out}  [dim]({fmt_clock(plan.duration)})[/]")
    return out


def _load_analyses(folder: Path, clips: list[ClipInfo]) -> dict[str, ClipAnalysis]:
    """Latest cached analysis per clip, whatever provider made it."""
    out: dict[str, ClipAnalysis] = {}
    adir = _workdir(folder) / "analysis"
    if not adir.exists():
        return out
    names = {c.name for c in clips}
    for f in sorted(adir.glob("*.json"), key=lambda p: p.stat().st_mtime):
        a = ClipAnalysis.from_dict(json.loads(f.read_text()))
        if a.clip in names:
            out[a.clip] = a
    return out


# --------------------------------------------------------------------------- commands


@app.command()
def run(
    folder: Path = FolderArg,
    source: list[Path] | None = SourceOpt,
    since: str | None = FromOpt,
    until: str | None = ToOpt,
    out: Path | None = typer.Option(None, "--out", "-o", help="Output file (default: next to the folder)."),
    provider: str = ProviderOpt,
    model: str = ModelOpt,
    duration: float = DurationOpt,
    aspect: str = AspectOpt,
    style: str = StyleOpt,
    music: Path | None = typer.Option(
        None, "--music", "-m", exists=True, dir_okay=False, help="Background track; auto-ducks under speech."
    ),
    captions: bool = typer.Option(True, help="Burn in the title and place-name captions."),
    subtitles: bool = typer.Option(False, help="Burn in subtitles of what you say (needs --provider gemini)."),
    quality: int = typer.Option(1080, help="Output short side in pixels (720 renders faster)."),
    force: bool = typer.Option(False, "--force", help="Re-analyse clips even if cached."),
    jobs: int = JobsOpt,
):
    """[bold]Everything[/]: scan → watch every clip → plan the story → render the video."""
    _check_aspect(aspect)
    clips = _scan(folder, source, since, until)
    prov, analyses = _analyze(folder, clips, provider, model, force, jobs)
    plan = _plan(prov, provider, clips, analyses, duration, aspect, style, folder)
    _render(folder, plan, clips, analyses, out or _default_out(folder, aspect), music, captions, subtitles, quality)
    console.print("[dim]Tweak the cut: edit .tripcut/plan.json, then `tripcut render` the folder again.[/]")


@app.command()
def analyze(
    folder: Path = FolderArg,
    source: list[Path] | None = SourceOpt,
    since: str | None = FromOpt,
    until: str | None = ToOpt,
    provider: str = ProviderOpt,
    model: str = ModelOpt,
    force: bool = typer.Option(False, "--force", help="Re-analyse clips even if cached."),
    jobs: int = JobsOpt,
):
    """Watch every clip and show what the AI found (cached, so later steps are free)."""
    clips = _scan(folder, source, since, until)
    _, analyses = _analyze(folder, clips, provider, model, force, jobs)
    _print_analyses(clips, analyses)


@app.command()
def plan(
    folder: Path = FolderArg,
    source: list[Path] | None = SourceOpt,
    since: str | None = FromOpt,
    until: str | None = ToOpt,
    provider: str = ProviderOpt,
    model: str = ModelOpt,
    duration: float = DurationOpt,
    aspect: str = AspectOpt,
    style: str = StyleOpt,
    jobs: int = JobsOpt,
):
    """Write [cyan].tripcut/plan.json[/] + storyboard + EDL without rendering. Edit, then `tripcut render`."""
    _check_aspect(aspect)
    clips = _scan(folder, source, since, until)
    prov, analyses = _analyze(folder, clips, provider, model, False, jobs)
    _plan(prov, provider, clips, analyses, duration, aspect, style, folder)


@app.command("render")
def render_cmd(
    folder: Path = FolderArg,
    out: Path | None = typer.Option(None, "--out", "-o"),
    music: Path | None = typer.Option(None, "--music", "-m", exists=True, dir_okay=False),
    captions: bool = typer.Option(True),
    subtitles: bool = typer.Option(False),
    quality: int = typer.Option(1080),
):
    """Render from the existing (possibly hand-edited) [cyan].tripcut/plan.json[/]."""
    plan_file = folder / ".tripcut" / "plan.json"
    if not plan_file.exists():
        _fail("No plan yet. Run `tripcut plan` (or `tripcut run`) first.")
    clips = _scan(folder)
    plan_obj = EditPlan.from_dict(json.loads(plan_file.read_text()))
    plan_obj, warnings = validate_plan(plan_obj, clips, target=0)
    for w in warnings:
        console.print(f"[dim]  · {w}[/]")
    analyses = _load_analyses(folder, clips)
    write_all(plan_obj, analyses, _workdir(folder))
    _render(
        folder,
        plan_obj,
        clips,
        analyses,
        out or _default_out(folder, plan_obj.aspect),
        music,
        captions,
        subtitles,
        quality,
    )


@app.command("finish")
def finish_cmd(
    masters: list[Path] = typer.Argument(..., help="Editor export(s), e.g. Palmier ProRes; several = parts in order."),
    out: Path = typer.Option(..., "--out", "-o", help="Final MP4 to write."),
    encoder: str = typer.Option("auto", help="[cyan]hardware[/] (Apple, ~1 min for 4K), [cyan]x265[/] (slow) or auto."),
    loudness: float = typer.Option(-14.0, help="Target integrated loudness in LUFS."),
    delete_masters: bool = typer.Option(False, help="Delete the masters once the final file is safely written."),
    wait: bool = typer.Option(False, help="Wait for each master to finish exporting before encoding it."),
    tmp_dir: Path | None = typer.Option(
        None, "--tmp-dir", file_okay=False, help="Where working files go (needs ~2-3x the result); default: next to -o."
    ),
):
    """Make the final 10-bit H.265 MP4 with mastered sound from editor exports; parts are joined frame-exact.

    Short on disk? Keep the master and [cyan]--tmp-dir[/] on a bigger drive; it checks space before starting.
    """
    if encoder not in ("auto", "hardware", "x265"):
        _fail("--encoder must be auto, hardware or x265")
    if encoder == "hardware" and not has_hardware_hevc():
        _fail("Apple's hardware HEVC encoder isn't available here; use --encoder x265.")
    if not wait:
        missing = [m.name for m in masters if not m.exists()]
        if missing:
            _fail(f"Not found: {', '.join(missing)} (use --wait if they're still exporting)")
    try:
        require_ffmpeg()
    except ToolMissingError as e:
        _fail(str(e))
    out.parent.mkdir(parents=True, exist_ok=True)
    try:
        with console.status("Starting…") as status:
            lufs, peak = finish(
                masters,
                out,
                encoder=encoder,
                target_lufs=loudness,
                delete_masters=delete_masters,
                wait=wait,
                tmp_dir=tmp_dir,
                on_step=lambda msg: status.update(msg + "…"),
            )
    except (FFmpegError, TimeoutError) as e:
        _fail(str(e))
    console.print(f"[green]✓[/] {out}  [dim]({lufs:.1f} LUFS, peak {peak:.1f} dBFS)[/]")


@app.command()
def version():
    """Print the version."""
    console.print(f"tripcut {__version__}")


def _check_aspect(aspect: str) -> None:
    if aspect not in ASPECTS:
        _fail(f"--aspect must be one of {', '.join(ASPECTS)}")


if __name__ == "__main__":  # pragma: no cover
    app()
