"""Local media analysis for the SSplayer routine-package authoring tool."""

from __future__ import annotations

import importlib.util
from concurrent.futures import ThreadPoolExecutor
from functools import lru_cache
import json
import math
import os
import re
import subprocess
import sys
import tempfile
import unicodedata
from pathlib import Path


SAMPLE_POSITIONS = ((0.14, "前段"), (0.48, "中段"), (0.80, "尾段"))

STRUCTURAL_REPEAT = re.compile(
    r"\b(?:REPEAT|RPT)\s+(?:(?:THE\s+|LUNGE\s+)?(?:BLOCKS?|SEQUENCES?|COMBOS?|SECTIONS?)\s+"
    r"(?:\d+|[A-Z][0-9¹²³⁴⁵⁶⁷⁸⁹⁰]*)(?!\w)|[A-Z][0-9]*\s*(?:[-–—&+]|AND|TO)\s*[A-Z][0-9]*\b)",
    re.IGNORECASE,
)


def unresolved_repeat_times(rows):
    """Identify structural shorthand that cannot be a finished cue row."""
    return [row["time"] for row in rows if any(
        STRUCTURAL_REPEAT.search(row.get(key) or "") for key in ("action", "coaching")
    )]


def run(*args):
    completed = subprocess.run(args, stdin=subprocess.DEVNULL, capture_output=True, check=False)
    if completed.returncode:
        reason = completed.stderr.decode(errors="replace").strip().splitlines()
        raise ValueError(f"{args[0]} failed: {reason[-1] if reason else completed.returncode}")
    return completed.stdout


def encode_with_progress(arguments, encoder, duration):
    print(f"视频编码 · {encoder}", file=sys.stderr, flush=True)
    command = [arguments[0], "-progress", "pipe:1", "-stats_period", "15", "-nostats", *arguments[1:]]
    with tempfile.TemporaryFile() as errors:
        with subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                              stderr=errors, text=True) as process:
            last_progress = 0
            try:
                for line in process.stdout:
                    key, _, value = line.strip().partition("=")
                    if key != "out_time_us" or not value.lstrip("-").isdigit():
                        continue
                    progress = min(100, int(int(value) / (duration * 10_000)))
                    if progress > last_progress:
                        print(f"视频已编码 {progress}%", file=sys.stderr, flush=True)
                        last_progress = progress
                status = process.wait()
            except BaseException:
                process.terminate()
                raise
        if status:
            errors.seek(0)
            details = errors.read().decode(errors="replace").strip().splitlines()
            reason = details[0] if details else str(status)
            raise ValueError(f"{encoder}: {reason}")


@lru_cache(maxsize=1)
def alignment_module():
    local = Path(__file__).with_name("rehearsal_alignment.py")
    parents = Path(__file__).resolve().parents
    source = parents[3] / "scripts" / "rehearsal_alignment.py" if len(parents) > 3 else local
    location = local if local.is_file() else source
    if not location.is_file():
        raise ValueError("Missing scripts/rehearsal_alignment.py. Download the complete public Skill folder.")
    spec = importlib.util.spec_from_file_location("rehearsal_alignment", location)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def probe(path, kind):
    result = json.loads(run("ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path)))
    streams = [s for s in result.get("streams", []) if s["codec_type"] == kind
               and not s.get("disposition", {}).get("attached_pic")]
    if not streams:
        raise ValueError(f"{path.name} has no {kind} stream")
    duration = float(result["format"]["duration"])
    if not math.isfinite(duration) or not 0 < duration <= 43200:
        raise ValueError(f"{path.name} has an invalid duration")
    return duration, result


def audio_packet_hash(path):
    return run("ffmpeg", "-nostdin", "-v", "error", "-xerror", "-i", str(path), "-map", "0:a:0",
               "-c", "copy", "-f", "streamhash", "-hash", "sha256", "-").strip()


def measure(path):
    result = subprocess.run(
        ["ffmpeg", "-nostdin", "-hide_banner", "-nostats", "-v", "info", "-xerror",
         "-i", str(path), "-map", "0:a:0", "-af", "ebur128=peak=true:framelog=verbose", "-f", "null", "-"],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, check=True,
    )
    summary = result.stderr.decode(errors="replace").rsplit("Integrated loudness:", 1)[-1]
    match = re.search(r"\bI:\s+(\S+) LUFS.*?True peak:\s+Peak:\s+(\S+) dBFS", summary, re.S)
    if not match:
        raise ValueError(f"No R128 measurement for {path.name}")
    integrated, peak = float(match[1]), float(match[2])
    if not math.isfinite(integrated) or not math.isfinite(peak):
        raise ValueError(f"No measurable audio in {path.name}")
    return integrated, peak


def balance_audio(source, destination, target, video=False):
    """Apply constant gain with headroom. Preserve the picture bitstream."""
    integrated, peak = measure(source)
    # This extra headroom leaves room for the encoder's reconstructed peaks.
    gain = min(target - integrated, -2.0 - peak, 12.0)
    if abs(gain) <= 0.5 and peak <= -1.5:
        return source, {"inputLUFS": integrated, "outputLUFS": integrated, "truePeakDBTP": peak, "gainDB": 0}
    if video:
        duration, media = probe(source, "video")
        audio = [s for s in media["streams"] if s["codec_type"] == "audio"]
        if len(audio) != 1:
            raise ValueError("Video balancing requires one audio stream. Select the intended version first.")
        args = ["-map", "0", "-c", "copy", "-c:a:0", "aac", "-b:a:0", "256k",
                "-filter:a:0", f"volume={gain:.6f}dB"]
    else:
        duration, media = probe(source, "audio")
        source_codec = next(s["codec_name"] for s in media["streams"] if s["codec_type"] == "audio")
        lossless = source_codec in {"alac", "flac", "wavpack"} or source_codec.startswith("pcm_")
        audio_encoder = ["-c:a", "alac"] if lossless else ["-c:a", "aac", "-b:a", "256k"]
        args = ["-map", "0:a:0", "-map_metadata", "0", *audio_encoder, "-af", f"volume={gain:.6f}dB"]
    run("ffmpeg", "-nostdin", "-v", "error", "-xerror", "-n", "-i", str(source), *args, str(destination))
    final_duration, final = probe(destination, "video" if video else "audio")
    if abs(final_duration - duration) > 0.20:
        raise ValueError(f"Audio balancing changed the duration of {source.name}")
    if video:
        # Compare all video packet timestamps and compressed payloads, not only container duration.
        entries = "packet=pts_time,dts_time,duration_time,size,data_hash"
        def picture_packets(path):
            return run("ffprobe", "-v", "error", "-select_streams", "v", "-show_packets",
                       "-show_data_hash", "sha256", "-show_entries", entries, "-of", "compact", str(path))
        if picture_packets(source) != picture_packets(destination):
            raise ValueError(f"Video balancing changed the encoded picture or its clock in {source.name}")
        initial_audio = next(s for s in media["streams"] if s["codec_type"] == "audio")
        final_audio = next(s for s in final["streams"] if s["codec_type"] == "audio")
        if abs(float(initial_audio.get("start_time", 0)) - float(final_audio.get("start_time", 0))) > 0.05:
            raise ValueError(f"Video balancing moved the embedded audio clock in {source.name}")
    final_lufs, final_peak = measure(destination)
    if final_peak > -1.4 or abs(final_lufs - integrated - gain) > 1.0:
        raise ValueError(f"Post-encode loudness check failed for {source.name}")
    return destination, {"inputLUFS": integrated, "outputLUFS": final_lufs, "truePeakDBTP": final_peak,
                         "gainDB": round(gain, 3)}


def picture_stream(media):
    return next(stream for stream in media["streams"] if stream["codec_type"] == "video"
                and not stream.get("disposition", {}).get("attached_pic"))


def video_info(path):
    duration, media = probe(path, "video")
    picture = picture_stream(media)
    size = path.stat().st_size
    bit_rate = str(picture.get("bit_rate", ""))
    return {"codec": picture["codec_name"], "codecTag": picture.get("codec_tag_string"),
            "width": picture["width"], "height": picture["height"],
            "bytes": size, "duration": duration, "bitrateKbps": round(size * 8 / duration / 1000),
            "pictureBitrateKbps": round(int(bit_rate) / 1000) if bit_rate.isdecimal() else None,
            "subtitleTracks": sum(stream["codec_type"] == "subtitle" for stream in media["streams"]),
            "closedCaptions": picture.get("closed_captions") == 1}


def video_timestamps(path):
    packets = run("ffprobe", "-v", "error", "-select_streams", "V:0", "-show_packets",
                  "-show_entries", "packet=pts_time", "-of", "csv=p=0", str(path))
    return sorted(float(line.split(b",")[0]) for line in packets.splitlines() if line.strip())


def stream_hashes(path, stream):
    return run("ffmpeg", "-nostdin", "-v", "error", "-xerror", "-i", str(path), "-map", f"0:{stream}",
               "-c", "copy", "-f", "streamhash", "-hash", "sha256", "-").strip()


def video_quality_samples(source, encoded, duration, width, height):
    length = min(2.0, duration / 3)
    normalize = "format=yuv420p,settb=AVTB,setpts=PTS-STARTPTS"
    graph = (f"[0:V:0]scale={width}:{height},{normalize}[reference];"
             f"[1:V:0]{normalize}[test];"
             "[reference][test]ssim=stats_file=-:shortest=1[comparison]")
    scores = []
    for position in (0.12, 0.50, 0.84):
        start = (duration - length) * position
        window = ("-ss", f"{start:.6f}", "-t", f"{length:.6f}")
        result = run("ffmpeg", "-nostdin", "-v", "error", *window, "-i", str(source),
                     *window, "-i", str(encoded), "-filter_complex_threads", "2",
                     "-filter_complex", graph, "-map", "[comparison]", "-an", "-f", "null", "-")
        frames = [float(value) for value in re.findall(rb"\bAll:(-?\d+(?:\.\d+)?)", result)]
        if not frames:
            raise ValueError("No comparable frames in a video quality sample.")
        scores.append(sum(frames) / len(frames))
    return scores


def efficient_video(source, destination, maximum_height, bitrate_kbps, codec):
    """Keep the smaller candidate only when its playback and sampled picture pass."""
    original = video_info(source)
    compatible = original["codec"] == codec and (codec != "hevc" or original.get("codecTag") == "hvc1")
    if maximum_height is None and bitrate_kbps is None and compatible:
        return source, {**original, "compression": "alreadyEfficient"}
    if bitrate_kbps is None and codec == "hevc":
        if existing_rate := original["pictureBitrateKbps"]:
            bitrate_kbps = max(250, round(existing_rate * 0.85))
    scores = []
    try:
        output, candidate = optimize_video(source, destination, maximum_height, bitrate_kbps, codec)
        if candidate["bytes"] > original["bytes"] * 0.90:
            raise ValueError("Video size savings were below 10%.")
        scores = video_quality_samples(source, output, original["duration"],
                                       candidate["width"], candidate["height"])
        if min(scores) < 0.96:
            raise ValueError("A sampled section did not meet the picture quality threshold.")
    except ValueError as error:
        destination.unlink(missing_ok=True)
        picture = {**original, "compression": "keptOriginal", "reason": str(error)}
        if scores:
            picture["sampleSSIM"] = [round(score, 5) for score in scores]
        return source, picture
    return output, {**candidate, "compression": "optimized", "sampleSSIM": [round(score, 5) for score in scores]}


def optimize_video(source, destination, maximum_height, bitrate_kbps, codec):
    """Keep every audio/subtitle stream and verify the video clock after compression."""
    if destination.exists() or destination.is_symlink():
        raise FileExistsError("Choose a new filename for the encoded video.")
    duration, before = probe(source, "video")
    pictures = [stream for stream in before["streams"] if stream["codec_type"] == "video"
                and not stream.get("disposition", {}).get("attached_pic")]
    if len(pictures) != 1:
        raise ValueError("Choose one camera stream or export with original video quality.")
    picture = pictures[0]
    if picture.get("color_transfer") in {"smpte2084", "arib-std-b67"} or picture.get("pix_fmt") not in {
        "yuv420p", "yuvj420p", "nv12", "yuv422p", "yuv444p",
    }:
        raise ValueError("Use original video quality for HDR or special picture formats.")
    if picture.get("closed_captions") == 1:
        raise ValueError("Use original video quality to preserve captions carried by the video stream.")

    software = "libx265" if codec == "hevc" else "libx264"
    hardware = "hevc_videotoolbox" if codec == "hevc" else "h264_videotoolbox"
    encoders = run("ffmpeg", "-hide_banner", "-encoders").decode(errors="replace")
    filter_graph = "format=yuv420p"
    if maximum_height is not None:
        if picture["height"] > maximum_height or picture["height"] % 2:
            filter_graph = f"scale=-2:'trunc(min(ih,{maximum_height})/2)*2',{filter_graph}"
    arguments = ["ffmpeg", "-nostdin", "-v", "error", "-xerror", "-n", "-copyts", "-i", str(source),
                 "-map", "0:V:0", "-map", "0:a?", "-map", "0:s?", "-map_metadata", "0", "-map_chapters", "0",
                 "-c:a", "copy", "-c:s", "copy", "-vf", filter_graph, "-fps_mode", "passthrough",
                 "-avoid_negative_ts", "disabled", "-movflags", "+faststart"]
    if bitrate_kbps is not None:
        arguments += ["-b:v", f"{bitrate_kbps}k", "-maxrate", f"{bitrate_kbps}k",
                      "-bufsize", f"{bitrate_kbps * 2}k"]
    if codec == "hevc":
        arguments += ["-tag:v", "hvc1"]
    remaining = []
    if sys.platform == "darwin" and hardware in encoders:
        settings = ["-allow_sw", "0"]
        if bitrate_kbps is None:
            settings += ["-q:v", "65"]
        remaining.append((hardware, settings))
    if software in encoders:
        settings = ["-preset", "fast", "-threads", "4"]
        if codec == "hevc":
            settings += ["-x265-params", "pools=4:frame-threads=2"]
        if bitrate_kbps is None:
            settings += ["-crf", "22" if codec == "hevc" else "20"]
        remaining.append((software, settings))
    if not remaining:
        raise ValueError(f"Install an FFmpeg build with a {codec} encoder or select original quality.")
    for encoder, settings in remaining:
        try:
            encode_with_progress([*arguments, "-c:v", encoder, *settings, str(destination)], encoder, duration)
            break
        except ValueError as error:
            destination.unlink(missing_ok=True)
            if encoder == remaining[-1][0]:
                raise
            print(f"{error}\n将尝试软件编码", file=sys.stderr, flush=True)

    final_duration, after = probe(destination, "video")
    old_pts, new_pts = video_timestamps(source), video_timestamps(destination)
    new_picture = picture_stream(after)
    if maximum_height is None:
        dimensions_match = ((new_picture["width"], new_picture["height"]) ==
                            (picture["width"], picture["height"]))
    else:
        dimensions_match = new_picture["height"] <= maximum_height
    if (abs(duration - final_duration) > 0.06 or not old_pts or len(old_pts) != len(new_pts)
            or max(abs(first - second) for first, second in zip(old_pts, new_pts)) > 0.005
            or not dimensions_match or new_picture["codec_name"] != codec):
        raise ValueError("Compression moved the video clock or missed the quality limit.")
    for kind, stream in (("audio", "a"), ("subtitle", "s")):
        old_streams = [stream for stream in before["streams"] if stream["codec_type"] == kind]
        new_streams = [stream for stream in after["streams"] if stream["codec_type"] == kind]
        if len(old_streams) != len(new_streams) or any(
            first["codec_name"] != second["codec_name"]
            or abs(float(first.get("start_time", 0)) - float(second.get("start_time", 0))) > 0.01
            or (first.get("tags", {}).get("language") or "und") != (second.get("tags", {}).get("language") or "und")
            for first, second in zip(old_streams, new_streams)
        ):
            raise ValueError(f"Compression changed a {kind} track or its start time.")
        if old_streams and stream_hashes(source, stream) != stream_hashes(destination, stream):
            raise ValueError(f"Compression changed the original {kind} packets.")
    return destination, {**video_info(destination), "encoder": encoder, "framesVerified": len(old_pts)}


def map_recordings(function, recordings):
    # Bound FFmpeg concurrency independently of batch size and preserve slot order.
    with ThreadPoolExecutor(max_workers=min(3, os.cpu_count() or 1)) as pool:
        return list(pool.map(function, recordings))


def decoded_window(path, start, seconds):
    import numpy as np

    data = run("ffmpeg", "-nostdin", "-v", "error", "-ss", f"{start:.6f}", "-i", str(path),
               "-t", f"{seconds:.6f}", "-map", "0:a:0", "-ac", "1", "-ar", "4000", "-af",
               "highpass=f=170,lowpass=f=1650", "-f", "f32le", "-")
    return np.frombuffer(data, dtype="<f4").astype(np.float64)


def timing_windows(song, audio, video, alignment):
    chapter = song.get("chapter")
    if not chapter or chapter["evidence"] != "audioMatched":
        return {"status": chapter["evidence"] if chapter else "unlocated", "windows": []}
    start, length = chapter["start"], song["duration"]
    width = min(5.0, length * 0.16)
    # Searching only near the claimed timestamp can lock onto a nearby beat
    # even when the same recording has a stronger match one or two beats away.
    margin = 2.5
    def measure_positions(positions):
        windows = []
        for part in positions:
            inside = length * part
            actual_margin = min(margin, start + inside)
            a = decoded_window(audio, inside, width)
            b = decoded_window(video, start + inside - actual_margin, width + 2 * actual_margin)
            if len(a) < 600 or len(b) < len(a):
                continue
            try:
                scores = alignment.normalized_waveform_correlation(b, a)
            except alignment.AlignmentError:
                continue
            peak = alignment.best_alignment_peak(scores, exclusion_frames=600)
            windows.append({"position": part,
                            "offsetMS": round((peak.offset_frames / 4000 - actual_margin) * 1000, 1),
                            "correlation": round(peak.score, 4),
                            "runnerUpGap": round(peak.score_gap or 0, 4)})
        return windows

    def passes(windows):
        if len(windows) != 3:
            return False
        offsets = [window["offsetMS"] for window in windows]
        error = max(abs(value) for value in offsets)
        drift = max(offsets) - min(offsets)
        scores = [window["correlation"] for window in windows]
        gaps = sorted(window["runnerUpGap"] for window in windows)
        standard = error <= 150 and drift <= 120 and min(scores) >= 0.10 and max(scores) >= 0.22
        # Voice-over lowers correlation. Require much tighter agreement before
        # accepting three distinct, lower-amplitude matches on the same clock.
        voiceover = error <= 25 and drift <= 12 and min(scores) >= 0.06 and max(scores) >= 0.13
        return gaps[1] >= 0.035 and (standard or voiceover)

    windows = measure_positions(part for part, _ in SAMPLE_POSITIONS)
    if passes(windows):
        return {"status": "measured", "windows": windows}
    # A song's chorus can create runner-up peaks one beat or phrase away.
    # Recheck fixed, non-overlapping positions across the whole recording.
    alternate = measure_positions((0.08, 0.36, 0.73))
    if passes(alternate):
        return {"status": "measured", "windows": alternate}
    return {"status": "review", "windows": windows}


def locate_chapters(video, rows, duration, audio_duration):
    alignment = alignment_module()
    chapters = [None] * len(rows)
    warnings = []
    eligible = []
    for index, item in enumerate(rows):
        if not 0.3 <= item["duration"] <= audio_duration:
            warnings.append(f"{item['section']} needs a manual chapter: the complete recording cannot fit.")
            continue
        section = re.fullmatch(r"T(\d+)([A-Z]?)", item["section"])
        probe = alignment.MusicTrack(int(section[1]), item["file"], item["duration"],
                                     segment=section[2] or None, variant=item["variant"])
        eligible.append((index, probe))
    if not eligible:
        return chapters, warnings
    # The shared spectral matcher has no authority over this PDF. Audio and
    # video correspondence is independently checked against the selected files.
    try:
        results, _ = alignment.align_tracks(video, [p for _, p in eligible], [], "ffmpeg",
                                            alignment.DEFAULT_DECODE_RATE, alignment.DEFAULT_FFT_SIZE,
                                            alignment.DEFAULT_HOP_SIZE, alignment.DEFAULT_BAND_COUNT, True)
    except alignment.AlignmentError as error:
        return chapters, warnings + [str(error)]
    for result, (index, _) in zip(results, eligible):
        if result.status == "unmatched":
            warnings.append(f"{rows[index]['section']} has no reliable match in the selected video.")
            continue
        row = rows[index]
        start = result.video_start_seconds
        end = start + row["duration"]
        reviewed = (result.status == "matched" and result.confidence == "high" and not
                    any("sequence" in w.lower() or "overlap" in w.lower() for w in result.warnings)
                    and 0 <= start < end <= duration)
        if not reviewed:
            warnings.append(f"{row['section']} has a provisional offset ({result.status}, {result.confidence}); review the exact recording before use.")
        chapters[index] = {
            "start": round(start, 4), "end": round(min(duration, end), 4),
            "evidence": "audioMatched" if reviewed else "approximate",
            "approved": False, "score": round(result.spectral_peak.score, 4),
            "runnerUpGap": round(result.spectral_peak.score_gap or 0, 4),
            "confidence": result.confidence, "status": result.status,
        } if 0 <= start < min(duration, end) else None
    candidates = [(index, chapter) for index, chapter in enumerate(chapters)
                  if chapter is not None and chapter["evidence"] == "audioMatched"]

    def verify_candidate(candidate):
        index, chapter = candidate
        track = rows[index]
        evidence = timing_windows({**track, "chapter": chapter}, track["file"], video, alignment)
        return index, evidence

    for index, evidence in map_recordings(verify_candidate, candidates):
        chapter = chapters[index]
        chapter["windows"] = evidence["windows"]
        chapter["approved"] = evidence["status"] == "measured"
        if not chapter["approved"]:
            chapter["evidence"] = "approximate"
            warnings.append(f"{rows[index]['section']} did not pass the three-window audio clock check.")
    return chapters, warnings


def row_lines(words):
    lines = []
    for word in sorted(words, key=lambda w: (w["top"], w["x0"])):
        if not lines or word["top"] - lines[-1][0]["top"] > 3:
            lines.append([])
        lines[-1].append(word)
    return [sorted(line, key=lambda w: w["x0"]) for line in lines]


def ocr_words(path, page, width):
    import csv
    with tempfile.TemporaryDirectory(prefix="ssroutine-ocr-") as temp:
        base = Path(temp) / "page"
        run("pdftoppm", "-f", str(page), "-l", str(page), "-r", "216", "-singlefile",
            "-png", str(path), str(base))
        table = run("tesseract", str(base.with_suffix(".png")), "stdout", "--psm", "11", "tsv").decode()
    rows = list(csv.DictReader(table.splitlines(), delimiter="\t"))
    scale = width / max(1, float(rows[0]["width"]))
    return [{"text": r["text"], "x0": float(r["left"]) * scale,
             "x1": (float(r["left"]) + float(r["width"])) * scale, "top": float(r["top"]) * scale}
            for r in rows if r["level"] == "5" and float(r["conf"]) >= 35 and r["text"].strip()]


def visible_level(page, position, top):
    fills = []
    for rectangle in page.rects:
        color = rectangle.get("non_stroking_color")
        if (rectangle.get("fill") and type(color) is tuple and len(color) == 3
                and rectangle["x0"] <= position <= rectangle["x1"]
                and rectangle["top"] + 1 <= top < rectangle["bottom"] - 1
                and max(color) - min(color) > 0.04 and all(0 <= c <= 1 for c in color)):
            fills.append(tuple(color))
    if len(set(fills)) == 1:
        return dict(zip(("red", "green", "blue"), fills[0]))
    return None


SEQUENCE_CODE = r"[A-Z][0-9¹²³⁴⁵⁶⁷⁸⁹⁰]{0,2}"
SEQUENCE_ITEM = re.compile(rf"({SEQUENCE_CODE})(?:\s*\((\d+\s*[x×])\))?")
SEQUENCE_REPEAT = re.compile(r"^(?:REPEAT|RPT)\s+(?:LUNGE\s+)?SEQUENCES?\s+(.+)$", re.S)


def expand_sequence(action, definitions):
    match = SEQUENCE_REPEAT.fullmatch(action)
    if not match:
        return None
    expression = " ".join(match[1].split())
    detail = re.search(r"[.,]\s*([LR] foot B|Leading [LR] hand first)\s*\.?$", expression, re.I)
    modifier = detail[1] if detail else ""
    identifiers = expression[:detail.start()] if detail else expression.rstrip(".")
    references = [SEQUENCE_ITEM.fullmatch(item.strip()) for item in identifiers.split(",")]
    if not all(references):
        return None
    result = []
    for item in references:
        source = definitions.get(unicodedata.normalize("NFKC", item[1]))
        if not source or unresolved_repeat_times([{"time": 0, "action": source}]):
            return None
        if modifier.lower().startswith("leading "):
            side = modifier.split()[1]
            source = re.sub(r"\bLeading [LR] hand\b", f"Leading {side} hand", source, flags=re.I)
        result.append(f"{source} ({item[2]})" if item[2] else source)
    return "\n".join(result), modifier


def read_page_rows(page, words, page_number, parse_time, definitions=None):
    if definitions is None:
        definitions = {}
    for line in row_lines(words):
        headers = {}
        for word in line:
            labels = re.split(r"[/|]", word["text"].upper())
            for label in labels:
                headers[label] = word
        if "MUSIC" not in headers or "EXERCISE" not in headers:
            continue
        if not any(h in headers for h in ("CTS", "REPS", "LEVEL")):
            continue
        def leading(key):
            return headers[key]["x0"] - 4 if key in headers else page.width
        music, exercise = leading("MUSIC"), leading("EXERCISE")
        sequence = headers.get("SEQUENCE")
        sequence_left = (min(sequence["x0"] - 4, exercise - 44)
                         if sequence is not None else exercise)
        counts, reps, level = (leading(key) for key in ("CTS", "REPS", "LEVEL"))
        if "CTS" in headers and "REPS" in headers and exercise < counts < reps:
            # Entries like AMRAP may be centered and extend left of the REPS label.
            right, left = headers["CTS"]["x1"], headers["REPS"]["x0"]
            if right < left:
                reps = (right + left) / 2
        floor = line[0]["top"] + 5
        # Captions and copyright at the bottom are outside the choreography table.
        words = [w for w in words if floor < w["top"] < page.height * 0.92]
        marks = sorted((w["top"], parse_time(w["text"])) for w in words
                       if w["x0"] < music and parse_time(w["text"]) is not None)
        rows = []
        for i, (top, time) in enumerate(marks):
            limit = marks[i + 1][0] - 3 if i + 1 < len(marks) else page.height * 0.92
            body = [w for w in words if top - 3 <= w["top"] < limit]
            def column(begin, end, top_bound=None, bottom_bound=None):
                return "\n".join(" ".join(w["text"] for w in line)
                                 for line in row_lines([w for w in body if begin <= w["x0"] < end
                                     and (top_bound is None or w["top"] >= top_bound)
                                     and (bottom_bound is None or w["top"] < bottom_bound)]))
            action_end = min((x for x in (counts, reps, level) if x > exercise), default=page.width)
            action = column(exercise, action_end)
            if action:
                codes = sorted((w for w in body if sequence_left <= w["x0"] < exercise
                                and re.fullmatch(SEQUENCE_CODE, w["text"])), key=lambda w: w["top"])
                for j, code in enumerate(codes):
                    start = code["top"] - 3
                    end = codes[j + 1]["top"] - 3 if j + 1 < len(codes) else limit
                    if text := column(exercise, action_end, start, end):
                        definitions[unicodedata.normalize("NFKC", code["text"])] = text
                cue = {"time": time, "page": page_number, "action": action,
                       "music": column(music, min(sequence_left, counts)),
                       "counts": column(counts, exercise if counts < exercise else min(reps, level)),
                       "repetitions": column(reps, level)}
                if expanded := expand_sequence(action, definitions):
                    cue["action"], qualifier = expanded
                    if qualifier:
                        cue["coaching"] = qualifier
                if color := visible_level(page, level + 6, top + 3):
                    cue["level"] = color
                rows.append(cue)
        return rows
    return []


def pdf_recording_variant(variant):
    return {"alternate": "alternative", "45-minute": "combo45"}.get(variant, variant)


def section_heading(words, width, alignment, parsed):
    """Read the numbered function title on a choreography table page."""
    if parsed is None and not alignment._has_cue_table_header(width, words):
        return None
    if alignment._has_modern_technique_and_coaching_heading(words):
        return None
    headings = []
    for line in row_lines(word for word in words if 85 <= word["top"] <= 125):
        numbered = [(index, match) for index, word in enumerate(line)
                    if (match := alignment.PDF_TRACK_RE.fullmatch(word["text"])
                        or alignment.PDF_INLINE_TRACK_RE.fullmatch(word["text"]))]
        if len(numbered) != 1:
            continue  # A combined 04A / 04B title needs manual review.
        index, number = numbered[0]
        if line[index]["x0"] >= width * 0.25:
            continue
        inline = number.groupdict().get("label")
        title_words = ([inline] if inline else []) + [word["text"] for word in line[index + 1:]]
        title = " ".join(" ".join(title_words).split())
        if not title or not any(character.isalpha() for character in title) or re.search(r"\b\d{1,2}:\d{2}", title):
            continue
        headings.append((number, title))
    if len(headings) != 1:
        return None
    number, title = headings[0]
    variant, source = alignment._classify_pdf_variant_with_source(words, width)
    if source == "structured" and variant == "alternate":
        title = title.split(" - ", 1)[0]
    title = re.sub(r"^(?:ALT\.?|ALTERNATE|BONUS|45[-–]MIN(?:UTE)?)[ :–-]+", "", title, flags=re.I)
    title = re.sub(r"\s+(?:\(?CONTINUED\)?|\(?CONT\.\)?)$", "", title, flags=re.I)
    title = re.sub(r"\s+\((?:ALT|ALTERNATE|BONUS)\)$", "", title, flags=re.I)
    if not title or len(title) > 68 or len(title.encode("utf-8")) > 240:
        return None
    return (int(number["number"]), (number["segment"] or "").upper(), pdf_recording_variant(variant)), title


def extract_cues(path, songs, ocr=False):
    import pdfplumber
    alignment = alignment_module()
    with pdfplumber.open(path) as pdf:
        words = {}
        found = []
        headings = {}
        for index, page in enumerate(pdf.pages, 1):
            positioned = page.extract_words()
            if not positioned and ocr:
                positioned = ocr_words(path, index, page.width)
            words[index] = positioned
            parsed = alignment.parse_cue_page_words(index, page.width, positioned)
            if parsed:
                found.append(parsed)
            if heading := section_heading(positioned, page.width, alignment, parsed):
                headings[index] = heading
        timelines, warnings = alignment._build_pdf_timelines(found)
        result = []
        for song in songs:
            section = re.fullmatch(r"T(\d+)([A-Z]?)", song["section"])
            recording = int(section[1]), section[2], song["variant"]
            candidates = [t for t in timelines if
                          (t.track_number, t.segment or "", pdf_recording_variant(t.variant)) == recording]
            pages = song.get("pdfPages", candidates[0].pages if len(candidates) == 1 else [])
            rows = []
            definitions = {}
            for n in pages:
                if type(n) is not int or n not in words:
                    raise ValueError(f"Invalid PDF page {n} for {song['section']}")
                rows.extend(read_page_rows(pdf.pages[n - 1], words[n], n, alignment.parse_timestamp, definitions))
            valid = all(0 <= row["time"] <= song["duration"] for row in rows)
            valid = valid and all(a["time"] <= b["time"] for a, b in zip(rows, rows[1:]))
            repeats = unresolved_repeat_times(rows)
            status = "unresolved"
            if valid and rows:
                status = "needsExpansion" if repeats else "review"
            result.append({"rows": rows, "pages": list(pages), "approved": False,
                           "status": status, "structuralRepeatTimes": repeats})
            selected = set(song.get("pdfPages") or ())
            if song.get("pdfPage"):
                selected.add(song["pdfPage"])
            proposed = [(page, title) for page, (identity, title) in headings.items()
                        if identity == recording and (not selected or page in selected)]
            if proposed and len({unicodedata.normalize("NFKC", title).casefold() for _, title in proposed}) == 1:
                page, title = proposed[0]
                result[-1]["sectionFunction"] = {"name": title, "page": page, "approved": False}
        return len(pdf.pages), result, warnings
