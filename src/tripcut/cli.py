"""tripcut command line."""

from __future__ import annotations

import json
from pathlib import Path

import typer
from rich.console import Console
from rich.progress import BarColumn, MofNCompleteColumn, Progress, SpinnerColumn, TextColumn, TimeElapsedColumn
from rich.table import Table

from . import __version__
from .analyze import DEFAULT_MODEL, analyze_clip, make_provider
from .export import fmt_clock, write_all
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

FolderArg = typer.Argument(..., exists=True, file_okay=False, resolve_path=True, help="Folder of clips from one trip.")
ProviderOpt = typer.Option(
    "gemini", "--provider", "-p", help="[cyan]gemini[/] (best) or [cyan]local[/] (no AI, no upload)."
)
ModelOpt = typer.Option(DEFAULT_MODEL, "--model", help="Gemini model name.")
DurationOpt = typer.Option(90.0, "--duration", "-d", help="Target length of the final video, in seconds.")
AspectOpt = typer.Option("16:9", "--aspect", "-a", help=f"One of {', '.join(ASPECTS)}. 9:16 for Reels/Shorts.")
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


def _scan(folder: Path) -> list[ClipInfo]:
    try:
        require_ffmpeg()
    except ToolMissingError as e:
        _fail(str(e))
    files, raw = find_clips(folder)
    files = [f for f in files if "-tripcut" not in f.stem]
    if raw:
        console.print(
            f"[yellow]![/] Skipping {len(raw)} raw Insta360 file(s) (.insv/.lrv). "
            "Export them as reframed MP4s first (Insta360 Studio or app → AI Frame / Auto Frame)."
        )
    if not files:
        _fail(f"No video files found in {folder}")

    clips: list[ClipInfo] = []
    with console.status(f"Reading {len(files)} clips…"):
        for f in files:
            try:
                clips.append(probe(f))
            except RuntimeError as e:
                console.print(f"[yellow]![/] {e}")
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


def _analyze(folder: Path, clips: list[ClipInfo], provider_name: str, model: str, force: bool):
    try:
        provider = make_provider(provider_name, model)
    except RuntimeError as e:
        _fail(str(e))
    wd = _workdir(folder)
    analyses: dict[str, ClipAnalysis] = {}
    cached = 0
    with Progress(
        SpinnerColumn(),
        TextColumn("[bold]{task.description}"),
        BarColumn(),
        MofNCompleteColumn(),
        TextColumn("[dim]{task.fields[clip]}"),
        TimeElapsedColumn(),
        console=console,
    ) as bar:
        task = bar.add_task(f"Watching clips ({provider.name})", total=len(clips), clip="")
        for c in clips:
            bar.update(task, clip=c.name)
            try:
                a, was_cached = analyze_clip(provider, c, wd, force=force)
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
):
    """[bold]Everything[/]: scan → watch every clip → plan the story → render the video."""
    _check_aspect(aspect)
    clips = _scan(folder)
    prov, analyses = _analyze(folder, clips, provider, model, force)
    plan = _plan(prov, provider, clips, analyses, duration, aspect, style, folder)
    _render(folder, plan, clips, analyses, out or _default_out(folder, aspect), music, captions, subtitles, quality)
    console.print("[dim]Tweak the cut: edit .tripcut/plan.json, then `tripcut render` the folder again.[/]")


@app.command()
def analyze(
    folder: Path = FolderArg,
    provider: str = ProviderOpt,
    model: str = ModelOpt,
    force: bool = typer.Option(False, "--force", help="Re-analyse clips even if cached."),
):
    """Watch every clip and show what the AI found (cached, so later steps are free)."""
    clips = _scan(folder)
    _, analyses = _analyze(folder, clips, provider, model, force)
    _print_analyses(clips, analyses)


@app.command()
def plan(
    folder: Path = FolderArg,
    provider: str = ProviderOpt,
    model: str = ModelOpt,
    duration: float = DurationOpt,
    aspect: str = AspectOpt,
    style: str = StyleOpt,
):
    """Write [cyan].tripcut/plan.json[/] + storyboard + EDL without rendering. Edit, then `tripcut render`."""
    _check_aspect(aspect)
    clips = _scan(folder)
    prov, analyses = _analyze(folder, clips, provider, model, False)
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


@app.command()
def version():
    """Print the version."""
    console.print(f"tripcut {__version__}")


def _check_aspect(aspect: str) -> None:
    if aspect not in ASPECTS:
        _fail(f"--aspect must be one of {', '.join(ASPECTS)}")


if __name__ == "__main__":  # pragma: no cover
    app()
