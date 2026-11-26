"""Local media analysis for the SSplayer routine-package authoring tool."""

from __future__ import annotations

import importlib.util
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from functools import lru_cache
from itertools import combinations, product
import json
import math
import os
import re
import subprocess
import sys
import tempfile
import unicodedata
from pathlib import Path

from score_evidence import (ScorePageReader, header_labels, normalized_label, numbered_ocr_heading,
                            row_lines)


SAMPLE_POSITIONS = ((0.14, "前段"), (0.48, "中段"), (0.80, "尾段"))

STRUCTURAL_REPEAT = re.compile(
    r"\b(?:REPEAT|RPT)\s+(?:(?:THE\s+|LUNGE\s+)?(?:BLOCKS?|SEQUENCES?|COMBOS?|SECTIONS?|PARTS?)\s+"
    r"(?:\d+|[A-Z][0-9¹²³⁴⁵⁶⁷⁸⁹⁰]*)(?!\w)|[A-Z][0-9]*\s*(?:[-–—&+]|AND|TO)\s*[A-Z][0-9]*\b)"
    r"|(?:重复|重做)\s*(?:第\s*)?(?:段落|部分|组合|区块|小节|段)\s*[A-Z0-9一二三四五六七八九十]",
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


def original_peak_notice(label, peak):
    if peak > 0:
        return (f"{label} 原始真峰值 {peak:+.1f} dBTP。已保留原音频。"
                "如需调整，请使用 App 的 Mix「歌单响度一致化」。")
    return None


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
    opposing = []

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
            offset = (peak.offset_frames / 4000 - actual_margin) * 1000
            gap = peak.score_gap or 0
            windows.append({"position": part, "offsetMS": round(offset, 1),
                            "correlation": round(peak.score, 4), "runnerUpGap": round(gap, 4)})
            if abs(offset) > 150 and peak.score >= 0.30 and gap >= 0.08:
                center = round(actual_margin * 4000)
                near = scores[max(0, center - 600):center + 601]
                clock_score = float(near.max()) if len(near) else 0
                if peak.score - clock_score >= 0.12:
                    opposing.append((part, offset, clock_score))
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

    def has_clock_conflict():
        # A clear displaced phrase or disjoint peaks with the same competing
        # offset may indicate a cut. Other agreeing phrases cannot overrule it.
        if any(score < 0.12 for _, _, score in opposing):
            return True
        return any(abs(left - right) * length >= width and abs(a - b) <= 120
                   for (left, a, _), (right, b, _) in combinations(opposing, 2))

    windows = measure_positions(part for part, _ in SAMPLE_POSITIONS)
    if passes(windows):
        return {"status": "measured", "windows": windows}
    # A song's chorus can create runner-up peaks one beat or phrase away.
    # Recheck fixed, non-overlapping positions across the whole recording.
    alternate = measure_positions((0.08, 0.36, 0.73))
    if has_clock_conflict():
        return {"status": "review", "windows": windows}
    if passes(alternate):
        return {"status": "measured", "windows": alternate}

    # Each set gets one extra independently positioned candidate per third.
    # Derive the clock with one set, then cross-check untouched windows with
    # the other set. Accept only each window's actual best peak, not the best
    # peak after steering the search toward the declared chapter start.
    def triples(points):
        regions = [[item for item in points if min(2, int(item["position"] * 3)) == region]
                   for region in range(3)]
        return (items for items in product(*regions) if passes(items))

    def strength(items):
        return sum(item["correlation"] for item in items)

    def agrees(discovered, checked):
        combined = sorted(discovered + checked, key=lambda item: item["position"])
        times = [item["position"] * length for item in combined]
        offsets = [item["offsetMS"] for item in combined]
        return (all(b - a >= width for a, b in zip(times, times[1:]))
                and max(offsets) - min(offsets) <= 120)

    discovered = max(triples(windows + measure_positions((0.11, 0.41, 0.88))), key=strength, default=())
    if not discovered:
        return {"status": "review", "windows": windows}
    checked = alternate + measure_positions((0.22, 0.59, 0.94))
    if has_clock_conflict():
        return {"status": "review", "windows": windows}
    verified = max((items for items in triples(checked) if agrees(discovered, items)), key=strength, default=())
    if verified:
        return {"status": "measured", "windows": list(verified)}
    return {"status": "review", "windows": windows}


def timing_failure_detail(evidence):
    windows = evidence["windows"]
    if not windows:
        return "没有能与视频音轨匹配的窗口"
    measurements = " / ".join(f"{window['position']:.0%} {window['offsetMS']:+.1f} ms"
                              f"（相关 {window['correlation']:.3f}，次峰差 {window['runnerUpGap']:.3f}）"
                              for window in windows)
    if len(windows) > 1:
        values = [window["offsetMS"] for window in windows]
        measurements += f" · 跨段偏差 {max(values) - min(values):.1f} ms"
    return measurements


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


def detached_score_footer(body, exercise, counts, page):
    if not exercise < counts < page.width:
        return body
    lines = row_lines(body)
    # A wide prose line after a clearly detached gap can cross all of the
    # table's columns. Keep the complete table above it, including sparse rows.
    for previous, current in zip(lines, lines[1:]):
        gap = current[0]["top"] - previous[0]["top"]
        if (gap >= page.width * 0.10 and len(current) >= 6
                and current[0]["top"] >= page.height * 0.79
                and exercise + page.width * 0.05 < current[0]["x0"] < counts <= current[-1]["x1"]):
            return [word for word in body if word["top"] < current[0]["top"] - 3]
    return body


def score_action_rows(actions, count_lines):
    result = []
    for line in count_lines:
        counts = " ".join(word["text"] for word in line)
        if re.fullmatch(r"\d{1,3}(?:\s*[x×]\s*\d{1,3})?", counts) is None:
            return []
        candidates = [index for index, action in enumerate(actions)
                      if abs(action[0]["top"] - line[0]["top"]) <= 3]
        if len(candidates) != 1 or (result and candidates[0] <= result[-1]["actionLine"]):
            return []
        result.append({"actionLine": candidates[0], "counts": counts})
    return result


def read_page_rows(page, words, page_number, parse_time, definitions=None):
    if definitions is None:
        definitions = {}
    for line in row_lines(words):
        headers = {}
        for word in line:
            for label in header_labels(word["text"]):
                headers[label] = word
        if "MUSIC" not in headers or "EXERCISE" not in headers:
            continue
        if not any(h in headers for h in ("CTS", "REPS", "LEVEL")):
            continue
        def leading(key):
            return headers[key]["x0"] - 4 if key in headers else page.width
        music, exercise = leading("MUSIC"), leading("EXERCISE")
        if not 0 <= music < exercise <= page.width:
            continue
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
            if i == len(marks) - 1:
                body = detached_score_footer(body, exercise, counts, page)
            def column_lines(begin, end, top_bound=None, bottom_bound=None):
                return row_lines([w for w in body if begin <= w["x0"] < end
                                  and (top_bound is None or w["top"] >= top_bound)
                                  and (bottom_bound is None or w["top"] < bottom_bound)])
            def column(begin, end, top_bound=None, bottom_bound=None):
                return "\n".join(" ".join(w["text"] for w in line)
                                 for line in column_lines(begin, end, top_bound, bottom_bound))
            action_end = min((x for x in (counts, reps, level) if x > exercise), default=page.width)
            count_end = exercise if counts < exercise else min(reps, level)
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
                       "counts": column(counts, count_end),
                       "repetitions": column(reps, level)}
                if expanded := expand_sequence(action, definitions):
                    cue["action"], qualifier = expanded
                    if qualifier:
                        cue["coaching"] = qualifier
                elif action_rows := score_action_rows(column_lines(exercise, action_end),
                                                      column_lines(counts, count_end)):
                    cue["actionRows"] = action_rows
                if color := visible_level(page, level + 6, top + 3):
                    cue["level"] = color
                rows.append(cue)
        if rows:
            return rows
    return []


def pdf_recording_variant(variant):
    return {"alternate": "alternative", "45-minute": "combo45"}.get(variant, variant)


@dataclass(frozen=True)
class SectionHeading:
    number: int
    segment: str
    variant: str
    variant_source: str
    title: str

    @property
    def recording(self):
        return self.number, self.segment, self.variant


def recording_identity(song):
    section = re.fullmatch(r"T(\d+)([A-Z]?)", song["section"])
    return int(section[1]), section[2], song["variant"]


def normalized_function_title(name):
    value = unicodedata.normalize("NFKC", name).casefold()
    value = re.sub(r"\s*[\u2010-\u2014\u2212-]\s*", "-", value)
    return " ".join(value.split())


LOCAL_NUMBERED_HEADING = re.compile(
    r"^(?:(?P<book>[A-Z][A-Z .&'’()\-]{1,45}\s+\d{1,4})\s*-\s*)?"
    r"(?P<number>\d{1,3})(?P<segment>[A-Z]?)(?:\s+|\s*[.、):]\s*|$)(?P<title>.*)$", re.I)
LOCAL_VARIANT = re.compile(r"(?:\s*\((备选|替换|加曲)(?:曲|版)?\)|\s+(备选曲|替换曲|加曲))$", re.I)
LOCAL_COACHING_TABLE = re.compile(r"\bTIME\s+(?:CNTS?\s+)?EXER?CISE\b", re.I)
NARRATIVE_HEADING = re.compile(
    r"^(?:TRACK FOCUS|FOCO DEL TRACK|FOCO DE LA PISTA|FOCUS DU MORCEAU|TRAININGSFOKUS|教学重点|训练重点|本节重点)(?:\b|\s|[:：])"
)


def localized_heading(text, following_title=""):
    if re.match(r"^\d{1,3}\s*[:.]\s*[0-5]\d(?:\s|$)", text):
        return None
    match = LOCAL_NUMBERED_HEADING.fullmatch(text)
    if not match or not 1 <= int(match["number"]) <= 128:
        return None
    title = match["title"].strip() or following_title.strip()
    if (not re.search(r"[\u3400-\u9fff]", title) or len(title) > 40
            or re.search(r"\d{1,2}\s*:\s*\d{2}", title)):
        return None
    variant, evidence = "standard", "default"
    if marker := LOCAL_VARIANT.search(title):
        variant = "bonus" if (marker[1] or marker[2]) == "加曲" else "alternative"
        evidence = "explicit"
        title = title[:marker.start()].strip()
    if title:
        return SectionHeading(int(match["number"]), match["segment"].upper(), variant, evidence, title)
    return None


def localized_section_headings(words, width):
    """Associate numbered roles with a localized action table on the same page."""
    lines = [(row[0]["x0"], row[0]["top"], unicodedata.normalize("NFKC", " ".join(w["text"] for w in row)))
             for row in row_lines(words)]
    headings = []
    first = 0
    for index, (_, top, text) in enumerate(lines):
        localized = len(text) <= 36 and re.search(r"(?:时间|时点|计时)", text) and re.search(r"(?:动作|练习)", text)
        if not (localized or LOCAL_COACHING_TABLE.search(text)):
            continue
        candidates = []
        for cursor in range(first, index):
            left, heading_top, heading = lines[cursor]
            if left >= width * 0.28 or not 0 < top - heading_top <= width * 0.44:
                continue
            following_title = ""
            if cursor + 1 < index:
                following_left, following_top, following = lines[cursor + 1]
                if left < following_left and 0 < following_top - heading_top <= 18:
                    following_title = following
            if candidate := localized_heading(heading, following_title):
                candidates.append(candidate)
        if len(candidates) == 1:
            headings.append(candidates[0])
        first = index + 1
    if not headings and lines and (cover := LOCAL_NUMBERED_HEADING.fullmatch(lines[0][2])) and cover["book"]:
        # A free-form lesson may repeat its heading in the body instead of
        # using a grid. Corroborate the exact role with the duration and at
        # least two exercise intervals before treating it as a role source.
        declared = localized_heading(lines[0][2])
        repeated = declared and any(localized_heading(text) == declared for _, _, text in lines[1:7])
        duration = any(re.match(r"^(?:训练时间|练习时间|训练时长|练习时长)\s*:", text) for _, _, text in lines[1:7])
        intervals = sum(bool(re.match(r"^\d{1,2}[.、]\s+.+?\s+\d+(?:['′’][sS]?|秒)", text))
                        for _, _, text in lines[1:])
        if repeated and duration and intervals >= 2:
            headings.append(declared)
    return headings


def choreography_headings(words, width, alignment, parsed):
    if heading := section_heading(words, width, alignment, parsed):
        return [heading]
    return localized_section_headings(words, width)


def heading_for_song(song, headings):
    recording = recording_identity(song)
    exact = [heading for heading in headings if heading.recording == recording]
    if len(exact) == 1:
        return exact[0]
    same_number = [heading for heading in headings if heading.number == recording[0]]
    if len(same_number) == 1:
        return same_number[0]
    if len(headings) == 1:
        return headings[0]
    return None


def has_track_narrative(words):
    """Recognize a coaching page even when its guidance is untimed prose."""
    lines = [" ".join(word["text"] for word in line) for line in row_lines(words)]
    for index, line in enumerate(lines):
        if NARRATIVE_HEADING.match(normalized_label(line)):
            return sum(len(re.findall(r"[^\W\d_]", body)) >= 12
                       for body in lines[index + 1:]) >= 3
    return False


def has_printed_action_clock(words, width, alignment):
    if alignment._has_cue_table_header(width, words):
        return True
    durations = []
    for line in row_lines(words):
        if line[0]["x0"] >= width * 0.25:
            continue
        text = unicodedata.normalize("NFKC", " ".join(word["text"] for word in line))
        # A recording length or a duration mentioned in prose is not a cue.
        if re.match(r"^\d{1,3}\s*[:.]\s*[0-5]\d\b(?!\s*(?:min(?:ute)?s?|分钟)\b)\s+\S", text, re.I):
            return True
        if re.match(r"^\d+\s*(?:secs?(?:onds?)?|mins?(?:utes?)?|秒|分钟)\s*[:|–—-]\s*\S", text, re.I):
            durations.append(line[0]["x0"])
    return any(abs(first - second) <= 6 for first, second in zip(durations, durations[1:]))


def section_heading(words, width, alignment, parsed):
    """Read a function from a choreography table or a numbered coaching page."""
    if parsed is None and not alignment._has_cue_table_header(width, words) and not has_track_narrative(words):
        return None
    if alignment._has_modern_technique_and_coaching_heading(words) or any(
            re.match(r"^(?:TECNICA Y ENSENANZA|TECHNIQUE ET COACHING)\b",
                     normalized_label(" ".join(word["text"] for word in line)))
            for line in row_lines(words) if line[0]["top"] < width * 0.35):
        return None
    headings = []
    title_lines = row_lines(word for word in words if 85 <= word["top"] <= 125)
    if candidate := numbered_ocr_heading(words, width, alignment.PDF_TRACK_RE, alignment.PDF_INLINE_TRACK_RE):
        title_lines = [candidate]
    for line in title_lines:
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
    markers = {normalized_function_title(word["text"]).upper().strip("()[]") for word in words
               if 60 <= word["top"] <= 125 and 0 <= word["x0"] < width * 0.65}
    alternate = bool(markers & {"ALT", "ALT.", "ALTERNATE"})
    bonus = "BONUS" in markers
    express = any(marker.startswith("45-MIN") for marker in markers)
    if alternate and bonus:
        variant, source = "", "ambiguous"
    elif express and (alternate or bonus):
        variant, source = ("combo45Alternative" if alternate else "combo45Bonus"), "explicit"
    elif alternate or bonus:
        variant, source = ("alternate" if alternate else "bonus"), "explicit"
    elif express:
        variant, source = "45-minute", "explicit"
    if source == "structured" and variant == "alternate":
        title = title.split(" - ", 1)[0]
    title = re.sub(r"^(?:ALT\.?|ALTERNATE|BONUS|45[-–]MIN(?:UTE)?)[ :–-]+", "", title, flags=re.I)
    title = re.sub(r"\s+(?:\(?CONTINUED\)?|\(?CONT\.\)?)$", "", title, flags=re.I)
    title = re.sub(r"\s+\((?:ALT|ALTERNATE|BONUS)\)$", "", title, flags=re.I)
    if not title or len(title) > 68 or len(title.encode("utf-8")) > 240:
        return None
    return SectionHeading(int(number["number"]), (number["segment"] or "").upper(),
                          pdf_recording_variant(variant), source, title)


def check_section_function(song, heading):
    function = song["sectionFunction"]
    number, segment, variant = recording_identity(song)
    location = f"{song['section']} ({variant}) 原谱第 {function['page']} 页"
    if heading is None:
        return "manual", f"{location} 的功能标题无法自动核对，需人工复核。"
    if heading.number != number or (segment and heading.segment and segment != heading.segment):
        return "contradiction", f"{location} 印有 T{heading.number}{heading.segment}，与所填小节不同。"
    if heading.variant_source == "explicit" and heading.variant != variant:
        return "contradiction", f"{location} 的明确版别是 {heading.variant}，与所填版别不同。"
    if heading.segment != segment or heading.variant != variant or heading.variant_source == "ambiguous":
        return "manual", f"{location} 的功能或录音对应需人工复核。"
    declared = normalized_function_title(function["name"])
    printed = normalized_function_title(heading.title)
    if declared != printed:
        possibly_partial = (declared.startswith(printed + " ") or printed.startswith(declared + " ")
                            or declared in printed.split(" / "))
        if not possibly_partial:
            return "contradiction", f"{location} 印有「{heading.title}」，与所填功能「{function['name']}」不同。"
    if declared != printed or heading.variant_source == "structured":
        return "manual", f"{location} 的功能或录音对应需人工复核。"
    return "matched", None


def named_grit_formats(text, page):
    """Read course-qualified references without assuming the package's track numbering."""
    header = re.compile(r"^GRIT\s+(ATHLETIC|CARDIO|STRENGTH)\s+FORMAT$", re.I)
    row = re.compile(
        r"^(\d{1,3})\s+GRIT\s+(ATHLETIC|CARDIO|STRENGTH)\s+(\d{1,4})"
        r"\s*\(\s*TRACK\s+(\d{1,3})([A-Z]?)\s*\)\s+(.+)$", re.I)
    total = re.compile(r"^Total\s+time\s*:\s*(\d{1,3}):([0-5]\d)$", re.I)
    suggestions, kind, references = [], None, []
    for raw in text.splitlines():
        line = " ".join(unicodedata.normalize("NFKC", raw).split())
        if match := header.fullmatch(line):
            kind, references = match[1].lower(), []
        elif kind and (match := row.fullmatch(line)):
            position, course, release, number, suffix, name = match.groups()
            if int(position) != len(references) + 1 or len(references) >= 64 or not 1 <= int(number) <= 128:
                kind = None
                continue
            references.append({"position": int(position), "sourceCourse": f"GRIT {course.upper()}",
                               "sourceRelease": str(int(release)), "sourceTrack": f"T{int(number)}{suffix.upper()}",
                               "name": name})
        elif kind and (match := total.fullmatch(line)):
            seconds = int(match[1]) * 60 + int(match[2])
            minutes = (seconds + 30) // 60
            if len(references) >= 2 and 5 <= minutes <= 180:
                suggestions.append({
                    "key": f"grit-{kind}", "name": f"GRIT {kind.title()}", "minutes": minutes,
                    "page": page, "sourcePages": [page], "slots": [],
                    "sourceTotalSeconds": seconds, "sourceTrackReferences": references,
                    "sourceReview": ["原谱按课型分别编号。请先按课型、期号和原谱曲号核对包内曲目，再填写排课小节。"],
                    "approved": False,
                })
            kind = None
        elif kind and references and line:
            if line[0].isdigit() or "(" in line or ")" in line:
                kind = None
            elif len(references[-1]["name"]) + len(line) < 160:
                references[-1]["name"] += " " + line
    return suggestions


def class_format_suggestions(path, pages=None):
    """Read printed class tables. An agent still verifies order and omissions."""
    if path is None:
        return []
    import pdfplumber
    from class_templates import (FormatLine, TOTAL_LINE, combine_equivalent_pages, format_columns,
                                 format_header, line_text, matches_printed_role, recheck_ocr_track_labels)
    from score_evidence import ScorePageReader
    token = r"0*\d{1,3}[A-Z]?"
    consecutive = rf"{token}(?:\s*[&+]\s*(?:TRACK\s*)?(?:{token}|[A-Z])\b)*"
    source_row = re.compile(
        rf"^(?:TRACK\s*({consecutive}(?:\s*(?:/|\bor\b)\s*(?:TRACK\s*)?{consecutive})*)"
        rf"|EXPRESS\s*(0*\d{{1,3}}))(?=\s|$)", re.I)
    separator = re.compile(r"\s*(?:/|\bor\b)\s*(?:TRACK\s*)?", re.I)
    chinese_header = re.compile(r"^(\d{1,3})\s*分钟\s*(?:课程)?(?:版本|课型|编排)\s*$")
    chinese_row = re.compile(r"^(?:第\s*)?(\d{1,3})(?:\s*小节)?[\s、.：:]+([^\d].+)$")
    structure_row = re.compile(r"^(\d{1,3})\s+(.+?)\s+\d{1,3}:(\d{2})$")
    structure_total = re.compile(r"^(?:TOTAL|合计|总时长).*?(\d{1,3}):(\d{2})$", re.I)

    def find_chinese_table(text, page):
        tables = []
        minutes, slots = None, []
        for raw in text.splitlines() + ["结束"]:
            line = unicodedata.normalize("NFKC", raw).strip()
            if not line:
                continue
            if minutes is not None:
                row = chinese_row.fullmatch(line)
                if row and 1 <= int(row[1]) <= 128 and any("\u3400" <= char <= "\u9fff" for char in row[2]):
                    slots.append({"sectionNumbers": [int(row[1])], "name": row[2].strip()})
                    continue
                if len(slots) >= 2 and len({slot["sectionNumbers"][0] for slot in slots}) == len(slots):
                    tables.append({"minutes": minutes, "page": page, "slots": slots,
                                   "_kind": "FORMAT", "_option": None})
                minutes, slots = None, []
            if (match := chinese_header.fullmatch(line)) and 5 <= int(match[1]) <= 180:
                minutes = int(match[1])
        return tables

    def find_numbered_structure(text, page):
        collecting, slots = False, []
        for raw in text.splitlines():
            line = unicodedata.normalize("NFKC", raw).strip()
            if line == "课程结构":
                collecting, slots = True, []
            elif collecting and (match := structure_row.fullmatch(line)):
                if int(match[1]) != len(slots) + 1 or len(slots) >= 64:
                    return []
                # This time column is per-song duration. Only the explicitly
                # labelled total supplies the template duration.
                slots.append({"sectionNumbers": [int(match[1])], "name": match[2].strip()})
            elif collecting and (match := structure_total.fullmatch(line)):
                if len(slots) >= 2 and 5 <= int(match[1]) <= 180 and int(match[2]) < 60:
                    return [{"minutes": int(match[1]), "page": page, "slots": slots,
                             "_kind": "FORMAT", "_option": None}]
                return []
        return []

    def find_tables(columns, page, height):
        tables, current = [], None
        for column in columns:
            next_section = next((index for index, line in enumerate(column)
                                 if format_header(line.text)
                                 or re.match(r"^(?:PLEASE NOTE|KEY)\b|^请注意", line.text, re.I)),
                                len(column))
            prefix = column[:next_section]
            # A table can flow from the bottom of one column into the next.
            # A tail with independent track starts and a printed total makes
            # it distinguishable from a sidebar or a different teaching list.
            tracks = [line for line in prefix if source_row.match(line.text)]
            if not (current and not current.get("_total")
                    and current["_top"] > height * 0.54
                    and len(tracks) >= 2 and 0 < tracks[0].top < height * 0.36
                    and any(TOTAL_LINE.match(line.text) for line in prefix)):
                current = None
            current = read_table_column(column, page, tables, current)
        for table in tables:
            table.pop("_total", None)
            table.pop("_top", None)
        return [table for table in tables if table["slots"] or table.get("sourceReview")]

    def read_table_column(column, page, tables, current):
        previous = row_left = unnumbered = None
        for raw in column:
            line = unicodedata.normalize("NFKC", raw.text).strip()
            preceding, previous = previous, raw
            if header := format_header(line):
                minutes, kind, option = header
                current = {"minutes": minutes, "page": page, "slots": [],
                           "_kind": kind, "_option": option, "_top": raw.top}
                tables.append(current)
                row_left = unnumbered = None
            elif re.match(r"^(?:Approx\.?\s*Time|PLEASE NOTE|TECHNICAL TEAM CREDIT|RELEASE FEEDBACK|"
                          r"GENERAL FEEDBACK|KEY|CLAVE|DANOS TU FEEDBACK|NOTA|TENGA EN CUENTA)\b|^请注意|^套路反馈", line, re.I):
                current = None
            elif current is not None and TOTAL_LINE.match(line):
                current["_total"] = True
                unnumbered = None
            elif current is not None and (match := source_row.match(line)):
                row_left, unnumbered = raw.left, None
                parts = separator.split(match[1].upper()) if match[1] else [match[2]]
                groups = []
                for part in parts:
                    group = []
                    for section in re.split(r"\s*[&+]\s*(?:TRACK\s*)?", part):
                        if explicit := re.fullmatch(r"(\d+)([A-Z]?)", section):
                            number, suffix = int(explicit[1]), explicit[2]
                        else:
                            suffix = section
                        group.append((number, suffix))
                    groups.append(group)
                recordings = [recording for group in groups for recording in group]
                numbers = list(dict.fromkeys(number for number, _ in recordings))
                codes = [f"T{number}{suffix}" for number, suffix in recordings]
                rest = line[match.end():].strip()
                has_consecutive_parts = any(len(group) > 1 for group in groups)
                unsupported_parts = has_consecutive_parts and (len(groups) != 1 or len(numbers) != 1
                    or len(set(codes)) != len(codes) or not all(suffix for _, suffix in recordings))
                leftover_selection = re.match(r"^(?:[&+]|/|\bor\b)\s*\(?\s*(?:TRACK\s*(?:\d|\()|\d)", rest, re.I)
                if unsupported_parts or leftover_selection:
                    current.setdefault("sourceReview", []).append(line)
                    continue
                if not all(1 <= number <= 128 for number in numbers):
                    current = None
                    continue
                slot = {"sectionNumbers": numbers}
                if any(code[-1].isalpha() for code in codes):
                    slot["sectionCodes"] = codes
                if has_consecutive_parts:
                    slot["selectionMode"] = "consecutive"
                if rest:
                    slot["name"] = rest
                if re.search(r"\bif time allows\b", rest, re.I):
                    slot["isOptional"] = True
                if match[2]:
                    slot.update(variantPreference="alternative", enforcesVariant=True,
                                requiredDurationMinutes=current["minutes"])
                current["slots"].append(slot)
            elif current is not None and (re.match(r"^TRACK\s*\(", line, re.I)
                                          or (raw.ocr_words and re.match(r"^TRACK\b", line, re.I))):
                current.setdefault("sourceReview", []).append(line)
            elif current is not None and current["slots"]:
                slot = current["slots"][-1]
                if re.match(r"^\(if time allows\)", line, re.I):
                    slot["isOptional"] = True
                if slot.get("name", "").endswith("/") and re.match(r"^Bonus\b", line, re.I):
                    slot["_bonus"] = line
                elif ("_total" not in current and row_left is not None and preceding
                      and raw.height > 0 and raw.left - row_left > 15 and line[0:1].isalpha()):
                    gap = raw.top - preceding.top
                    wrap_gap = max(11, preceding.height * 1.38)
                    if 0 < gap <= wrap_gap:
                        if unnumbered is not None:
                            unnumbered[1].append(line)
                        elif "name" in slot and len((slot["name"] + " " + line).encode("utf-8")) <= 160:
                            slot["name"] += " " + line
                    elif wrap_gap < gap < wrap_gap * 2.35:
                        unnumbered = (len(current["slots"]), [line])
                        current.setdefault("_unnumbered", []).append(unnumbered)
        return current

    guesses, named = [], []
    reader = ScorePageReader(path)
    with pdfplumber.open(path) as source:
        numbers = range(1, len(source.pages) + 1) if pages is None else sorted(set(pages))
        for number in numbers:
            if not 1 <= number <= len(source.pages):
                raise ValueError(f"Original course PDF has no physical page {number}.")
            page = source.pages[number - 1]
            words = reader.words(page, number)
            uses_ocr = any(word.get("ocr") for word in words)
            text = ("\n".join(map(line_text, row_lines(words))) if uses_ocr else page.extract_text()) or ""
            normalized = unicodedata.normalize("NFKC", text)
            has_format = bool(re.search(r"format|分钟|小节设置|MINUTOS?\b|MIN(?:UTE)?S?\s+(?:STRENGTH|FLEXIBILITY)\b",
                                        normalized, re.I))
            has_chinese = "分钟" in normalized
            if not (has_format or "课程结构" in normalized):
                continue
            if "课程结构" in normalized:
                guesses.extend(find_numbered_structure(text, number))
            if not has_format:
                continue
            midpoint = (page.bbox[0] + page.bbox[2]) / 2
            if uses_ocr:
                left, right = ("\n".join(map(line_text, row_lines(
                    [word for word in words if (word["x0"] < midpoint) == is_left]))) for is_left in (True, False))
            else:
                left = page.crop((page.bbox[0], page.bbox[1], midpoint, page.bbox[3])).extract_text() or ""
                right = page.crop((midpoint, page.bbox[1], page.bbox[2], page.bbox[3])).extract_text() or ""
            columns = format_columns(words, page.width, row_lines, source_row)
            if columns:
                if uses_ocr:
                    recheck_ocr_track_labels(columns, path, number, source_row)
                guesses.extend(find_tables(columns, number, page.height))
            if "format" in normalized.lower():
                if not columns:
                    guesses.extend(find_tables([list(map(FormatLine, left.splitlines())),
                                                list(map(FormatLine, right.splitlines()))], number, page.height)
                                   or find_tables([list(map(FormatLine, text.splitlines()))], number, page.height))
                named_columns = named_grit_formats(left, number) + named_grit_formats(right, number)
                for template in named_columns or named_grit_formats(text, number):
                    duplicate = next((item for item in named if item["key"] == template["key"]
                                      and item["sourceTotalSeconds"] == template["sourceTotalSeconds"]
                                      and item["sourceTrackReferences"] == template["sourceTrackReferences"]), None)
                    if duplicate is None:
                        named.append(template)
                    elif number not in duplicate["sourcePages"]:
                        duplicate["sourcePages"].append(number)
            if has_chinese:
                columns = find_chinese_table(left, number) + find_chinese_table(right, number)
                guesses.extend(columns or find_chinese_table(text, number))
        if any(table.get("_unnumbered") for table in guesses):
            alignment = alignment_module()
            numbered_roles = [heading for index, page in enumerate(source.pages, 1)
                              for heading in choreography_headings(reader.words(page, index), page.width, alignment, None)
                              if heading.variant == "standard"]
            for table in guesses:
                for position, lines in reversed(table.pop("_unnumbered", [])):
                    label = " ".join(lines)
                    matches = {(heading.number, heading.segment) for heading in numbered_roles
                               if matches_printed_role(label, heading.title)}
                    if len(matches) == 1 and len(label.encode("utf-8")) <= 160:
                        number, segment = matches.pop()
                        slot = {"sectionNumbers": [number], "name": label}
                        if segment:
                            slot["sectionCodes"] = [f"T{number}{segment}"]
                        table["slots"].insert(position, slot)
                    else:
                        table.setdefault("sourceReview", []).append(f"未标曲号的原谱栏位 · {label}")
    for item in guesses:
        page_slots = [slot for table in guesses if table["page"] == item["page"] for slot in table["slots"]]
        for slot in item["slots"]:
            if bonus := slot.pop("_bonus", None):
                matches = {candidate["sectionNumbers"][0] for candidate in page_slots
                           if len(candidate["sectionNumbers"]) == 1
                           and normalized_function_title(candidate.get("name", "")) == normalized_function_title(bonus)}
                if len(matches) == 1 and not slot.get("sectionCodes"):
                    slot["sectionNumbers"] = list(dict.fromkeys([*slot["sectionNumbers"], *matches]))
                    slot["name"] = slot["name"].rstrip("/ ") + " / " + bonus
    guesses = combine_equivalent_pages(guesses)
    counts = {}
    for template in guesses:
        minutes = template["minutes"]
        counts[minutes] = counts.get(minutes, 0) + 1
        kind, option = template.pop("_kind"), template.pop("_option")
        suffix = chr(64 + counts[minutes]) if sum(t["minutes"] == minutes for t in guesses) > 1 else ""
        label = f"{minutes} 分钟"
        if kind in {"STRENGTH", "FLEXIBILITY"}:
            label += f" {kind.title()}"
            suffix = ""
        elif any(slot.get("enforcesVariant") for slot in template["slots"]):
            label += " Express"
        if option:
            suffix = f"方案 {option}"
        template.update(key=f"{minutes}-{counts[minutes]}", name=f"{label} {suffix}".strip(), approved=False)
    return sorted([*guesses, *named], key=lambda item: item["page"])


def inspect_section_functions(path, songs):
    claimed = [song for song in songs if song.get("sectionFunction")]
    if not claimed:
        return []
    import pdfplumber
    alignment = alignment_module()
    reader = ScorePageReader(path)
    with pdfplumber.open(path) as pdf:
        headings = {}
        for index in {song["sectionFunction"]["page"] for song in claimed}:
            if not 1 <= index <= len(pdf.pages):
                raise ValueError(f"The actual PDF does not contain section function page {index}.")
            page = pdf.pages[index - 1]
            headings[index] = choreography_headings(reader.words(page, index), page.width, alignment, None)
        return [check_section_function(song, heading_for_song(song, headings[song["sectionFunction"]["page"]]))
                for song in claimed]


def _source_timelines(song, timelines, parsed_pages):
    number, segment, variant = recording_identity(song)
    matching = [timeline for timeline in timelines if timeline.track_number == number
                and (timeline.segment or "") == segment]
    standard = [timeline for timeline in matching if pdf_recording_variant(timeline.variant) == variant]
    cue_pages = {row["page"] for row in song.get("cues") or ()}
    opening = song.get("pdfPage") or (song.get("sectionFunction") or {}).get("page")
    pinned = cue_pages or ({opening} if opening else set())
    if not pinned:
        return standard
    # A second translation restarts the same score clock. It may supply the
    # exact pinned pages, but never changes the explicit recording variant.
    identified = [timeline for timeline in matching if pinned.issubset(timeline.pages) and (
        timeline in standard or (timeline.variant_source == "clock-reset" and all(
            pdf_recording_variant(parsed_pages[page].variant) == variant for page in timeline.pages)))]
    return identified if len(identified) == 1 else standard


def _rows_on_pages(document, words, pages, parse_time):
    rows, definitions = [], {}
    for number in pages:
        if type(number) is not int or number not in words:
            raise ValueError(f"Invalid physical PDF page {number}")
        rows.extend(read_page_rows(document.pages[number - 1], words[number], number, parse_time, definitions))
    return rows


def extract_cues(path, songs, ocr=False, *, page_numbers=None):
    """Use physical source pages. The older OCR flag remains compatible with automatic fallback."""
    import pdfplumber
    alignment = alignment_module()
    reader = ScorePageReader(path)
    with pdfplumber.open(path) as pdf:
        words = {}
        found = []
        headings = {}
        numbers = range(1, len(pdf.pages) + 1) if page_numbers is None else sorted(set(page_numbers))
        for index in numbers:
            if type(index) is not int or not 1 <= index <= len(pdf.pages):
                raise ValueError(f"Invalid physical PDF page {index}")
            page = pdf.pages[index - 1]
            positioned = reader.words(page, index)
            words[index] = positioned
            parsed = alignment.parse_cue_page_words(index, page.width, positioned)
            if parsed:
                found.append(parsed)
            if page_headings := choreography_headings(positioned, page.width, alignment, parsed):
                headings[index] = page_headings
        timelines, warnings = alignment._build_pdf_timelines(found)
        parsed_pages = {page.page_number: page for page in found}
        named_pages = sorted(headings)
        result = []
        for song in songs:
            recording = recording_identity(song)
            candidates = _source_timelines(song, timelines, parsed_pages)
            pages = song.get("pdfPages", candidates[0].pages if len(candidates) == 1 else [])
            rows = _rows_on_pages(pdf, words, pages, alignment.parse_timestamp)
            pinned = sorted({row["page"] for row in song.get("cues") or ()})
            if ("pdfPages" not in song and len(rows) >= 3 and pinned and
                    all(number in words and number not in parsed_pages for number in pinned)):
                # Some translations render a correct table with an unreadable
                # numbered heading. It must reproduce the complete known
                # track clock before its physical pages can replace the draft.
                translated = _rows_on_pages(pdf, words, pinned, alignment.parse_timestamp)
                if [row["time"] for row in translated] == [row["time"] for row in rows]:
                    pages, rows = pinned, translated
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
            proposed = [(page, heading.title) for page, page_headings in headings.items() for heading in page_headings
                        if heading.recording == recording and (not selected or page in selected)]
            if proposed and len({normalized_function_title(title) for _, title in proposed}) == 1:
                page, title = proposed[0]
                result[-1]["sectionFunction"] = {"name": title, "page": page, "approved": False}
            # A timed course may have one untimed section. Verify the exact
            # numbered opening and its continuation pages, never the whole PDF.
            function = song.get("sectionFunction") or result[-1].get("sectionFunction") or {}
            opening = song.get("pdfPage") or function.get("page")
            if opening in headings and not rows and not candidates and has_track_narrative(words[opening]):
                matched_headings = [heading for heading in headings[opening]
                                    if heading.recording == recording and heading.variant_source != "ambiguous"]
                if len(matched_headings) == len(headings[opening]) == 1:
                    following = next((index for index in named_pages if index > opening
                                      and not any(heading.recording == recording for heading in headings[index])),
                                     len(pdf.pages) + 1)
                    related = set(range(opening, following))
                    related.update(index for index, items in headings.items()
                                   if any(heading.recording == recording for heading in items))
                    if all(index in words and len(words[index]) >= 12
                           and not has_printed_action_clock(words[index], pdf.pages[index - 1].width, alignment)
                           for index in related):
                        result[-1]["untimedScorePage"] = opening
            if function := song.get("sectionFunction"):
                heading = heading_for_song(song, headings.get(function["page"], ()))
                result[-1]["sectionFunctionCheck"] = check_section_function(song, heading)
        return len(pdf.pages), result, [*warnings, *reader.warnings]
