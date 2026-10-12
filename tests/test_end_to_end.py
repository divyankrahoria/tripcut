import json
import subprocess

from typer.testing import CliRunner

from tripcut.cli import app
from tripcut.ffmpeg import make_proxy
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
def test_proxy_of_10bit_footage_is_8bit(tmp_path):
    src = tmp_path / "tenbit.mkv"
    subprocess.run(
        ["ffmpeg", "-loglevel", "error", "-y", "-f", "lavfi", "-i", "testsrc2=size=640x360:rate=30:duration=1"]
        + ["-c:v", "ffv1", "-pix_fmt", "yuv420p10le", str(src)],
        check=True,
    )
    proxy = make_proxy(src, tmp_path / "proxy.mp4")
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v", "-show_entries", "stream=pix_fmt", "-of", "csv=p=0"]
        + [str(proxy)],
        capture_output=True,
        text=True,
        check=True,
    )
    assert out.stdout.strip() == "yuv420p"


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


def _stream(path, entries: str) -> list[str]:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", f"stream={entries}", "-of", "csv=p=0", str(path)],
        capture_output=True,
        text=True,
        check=True,
    )
    return out.stdout.split()


@needs_ffmpeg
def test_finish_joins_parts_frame_exact_and_mastered(tmp_path):
    # Two editor "masters" whose audio runs a little long / short, like real exports.
    parts = []
    for name, seconds, audio_seconds in (("a", 2, 2.1), ("b", 1.5, 1.4)):
        part = tmp_path / f"{name}.mov"
        subprocess.run(
            [
                "ffmpeg",
                "-loglevel",
                "error",
                "-y",
                "-f",
                "lavfi",
                "-i",
                f"testsrc2=size=320x180:rate=30:duration={seconds}",
            ]
            + ["-f", "lavfi", "-i", f"sine=frequency=330:sample_rate=48000:duration={audio_seconds}"]
            + ["-c:v", "prores_ks", "-c:a", "pcm_s16le", str(part)],
            check=True,
        )
        parts.append(part)
    out = tmp_path / "final.mp4"
    result = runner.invoke(app, ["finish", *map(str, parts), "-o", str(out), "--encoder", "x265", "--delete-masters"])
    assert result.exit_code == 0, result.output

    codec, pix_fmt, frames = _stream(out, "codec_name,pix_fmt,nb_frames")[0].split(",")
    assert (codec, pix_fmt, int(frames)) == ("hevc", "yuv420p10le", 105)
    audio = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "a", "-show_entries", "stream=duration", "-of", "csv=p=0"]
        + [str(out)],
        capture_output=True,
        text=True,
        check=True,
    )
    assert abs(float(audio.stdout) - 3.5) < 0.05  # audio follows the picture: 2.0 s + 1.5 s
    assert "LUFS" in result.output and not any(p.exists() for p in parts)
    from tripcut.finish import measure_loudness

    lufs, peak = measure_loudness(out)
    assert abs(lufs + 14) < 1.0 and peak <= -0.5


@needs_ffmpeg
def test_clips_on_other_drives_with_date_range_are_remembered(tmp_path):
    from .conftest import make_broll_clip

    camera, card, trip = tmp_path / "camera", tmp_path / "card", tmp_path / "trip"
    camera.mkdir()
    card.mkdir()
    make_broll_clip(camera / "VID_a.mp4", seconds=3, when="2026-06-30T09:00:00Z")
    make_broll_clip(camera / "VID_old.mp4", seconds=3, when="2026-05-01T09:00:00Z")  # an earlier trip
    make_broll_clip(card / "VID_b.mp4", seconds=3, when="2026-07-10T09:00:00Z")
    (card / "._VID_b.mp4").write_bytes(b"\0" * 4096)  # macOS metadata junk on exFAT drives
    (card / "LRV_b.lrv").write_bytes(b"\0" * 4096)  # Insta360 low-res preview

    args = ["analyze", str(trip), "-p", "local", "-S", str(camera), "-S", str(card)]
    result = runner.invoke(app, [*args, "--from", "2026-06-30", "--to", "2026-07-10"])
    assert result.exit_code == 0, result.output
    assert "2 clips" in result.output and "Skipping" not in result.output
    assert not (camera / ".tripcut").exists() and (trip / ".tripcut" / "analysis").is_dir()

    again = runner.invoke(app, ["analyze", str(trip), "-p", "local"])  # sources and dates remembered
    assert again.exit_code == 0, again.output
    assert "2 clips" in again.output


@needs_ffmpeg
def test_finish_refuses_a_black_master_and_keeps_it(tmp_path):
    # What an editor exports when it has lost access to its clips: black picture, titles and music only.
    master = tmp_path / "black.mov"
    subprocess.run(
        ["ffmpeg", "-loglevel", "error", "-y", "-f", "lavfi", "-i", "color=black:size=320x180:rate=30:duration=3"]
        + ["-f", "lavfi", "-i", "sine=frequency=330:sample_rate=48000:duration=3"]
        + ["-c:v", "prores_ks", "-c:a", "pcm_s16le", str(master)],
        check=True,
    )
    out = tmp_path / "final.mp4"
    result = runner.invoke(app, ["finish", str(master), "-o", str(out), "--encoder", "x265", "--delete-masters"])
    assert result.exit_code == 1
    assert "black" in result.output
    assert master.exists() and not out.exists()


@needs_ffmpeg
def test_finish_checks_space_first_and_keeps_masters(tmp_path, monkeypatch):
    import shutil as _shutil

    master = tmp_path / "m.mov"
    subprocess.run(
        ["ffmpeg", "-loglevel", "error", "-y", "-f", "lavfi", "-i", "testsrc2=size=320x180:rate=30:duration=2"]
        + ["-f", "lavfi", "-i", "sine=frequency=330:sample_rate=48000:duration=2"]
        + ["-c:v", "prores_ks", "-c:a", "pcm_s16le", str(master)],
        check=True,
    )
    real = _shutil.disk_usage
    monkeypatch.setattr(_shutil, "disk_usage", lambda p: real(p)._replace(free=1000))  # a full disk
    out = tmp_path / "final.mp4"
    result = runner.invoke(app, ["finish", str(master), "-o", str(out), "--encoder", "x265", "--delete-masters"])
    assert result.exit_code == 1 and "Not enough space" in result.output
    assert master.exists() and not out.exists()
