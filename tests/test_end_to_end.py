import json
import subprocess

from typer.testing import CliRunner

from tripcut.cli import app
from tripcut.probe import find_clips, probe

from .conftest import needs_ffmpeg

runner = CliRunner()


def duration(path) -> float:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
        capture_output=True,
        text=True,
        check=True,
    )
    return float(out.stdout)


@needs_ffmpeg
def test_probe_reads_dates_audio_and_360(trip):
    files, _ = find_clips(trip)
    info = {p.name: probe(p) for p in files}
    assert info["b_intro.mp4"].created_source == "metadata"
    assert info["VID_20260913_181500_00_007.mp4"].created_source == "filename"
    assert info["VID_20260913_181500_00_007.mp4"].has_audio is False
    assert info["raw360.mp4"].is_360 is True
    assert info["a_harbour.mp4"].is_360 is False


@needs_ffmpeg
def test_local_pipeline_end_to_end(trip, tmp_path):
    out = tmp_path / "out.mp4"
    result = runner.invoke(app, ["run", str(trip), "-p", "local", "-d", "25", "--quality", "360", "-o", str(out)])
    assert result.exit_code == 0, result.output
    assert out.exists()

    wd = trip / ".tripcut"
    plan = json.loads((wd / "plan.json").read_text())
    used = {s["clip"] for s in plan["segments"]}
    assert "raw360.mp4" not in used  # unreframed 360 is skipped
    assert {"a_harbour.mp4", "b_intro.mp4", "c_tram.mp4"} <= used
    expected = sum(s["end"] - s["start"] for s in plan["segments"])
    assert abs(duration(out) - expected) < 0.5
    assert (wd / "storyboard.md").exists() and (wd / "timeline.edl").exists()

    # Hand-edit the plan (keep only the first two shots, vertical) and re-render.
    plan["segments"] = plan["segments"][:2]
    plan["aspect"] = "9:16"
    (wd / "plan.json").write_text(json.dumps(plan))
    out2 = tmp_path / "short.mp4"
    result = runner.invoke(app, ["render", str(trip), "--quality", "360", "-o", str(out2), "--no-captions"])
    assert result.exit_code == 0, result.output
    w, h = (
        subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-select_streams",
                "v",
                "-show_entries",
                "stream=width,height",
                "-of",
                "csv=p=0",
                str(out2),
            ],
            capture_output=True,
            text=True,
            check=True,
        )
        .stdout.strip()
        .split(",")
    )
    assert int(h) > int(w)


@needs_ffmpeg
def test_music_mix(trip, tmp_path):
    music = tmp_path / "song.m4a"
    subprocess.run(
        ["ffmpeg", "-loglevel", "error", "-y", "-f", "lavfi", "-i", "sine=f=330:d=5", "-c:a", "aac", str(music)],
        check=True,
    )
    out = tmp_path / "with_music.mp4"
    result = runner.invoke(
        app, ["run", str(trip), "-p", "local", "-d", "15", "--quality", "360", "-o", str(out), "-m", str(music)]
    )
    assert result.exit_code == 0, result.output
    assert duration(out) > 10  # short song was looped, not truncating the video
