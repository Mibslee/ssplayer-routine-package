"""Local media analysis for the SSplayer routine-package authoring tool."""

from __future__ import annotations

import importlib.util
import json
import math
import re
import subprocess
import sys
import tempfile
from pathlib import Path


def run(*args):
    completed = subprocess.run(args, stdin=subprocess.DEVNULL, capture_output=True, check=False)
    if completed.returncode:
        reason = completed.stderr.decode(errors="replace").strip().splitlines()
        raise ValueError(f"{args[0]} failed: {reason[-1] if reason else completed.returncode}")
    return completed.stdout


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
    result = subprocess.run(["ffmpeg", "-nostdin", "-hide_banner", "-nostats", "-v", "info", "-xerror",
                             "-i", str(path), "-map", "0:a:0", "-af",
                             "loudnorm=I=-16:TP=-1.5:LRA=11:print_format=json", "-f", "null", "-"],
                            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, check=True)
    match = re.search(r'\{\s*"input_i"\s*:.*?\}', result.stderr.decode(errors="replace"), re.S)
    if not match:
        raise ValueError(f"No R128 measurement for {path.name}")
    values = json.loads(match[0])
    integrated, peak = float(values["input_i"]), float(values["input_tp"])
    if not math.isfinite(integrated) or not math.isfinite(peak):
        raise ValueError(f"No measurable audio in {path.name}")
    return integrated, peak


def balance_audio(source, destination, target, video=False):
    """Apply constant gain with headroom. Preserve the picture bitstream."""
    integrated, peak = measure(source)
    # This extra headroom leaves room for the encoder's reconstructed peaks.
    gain = min(target - integrated, -2.0 - peak, 12.0)
    if abs(gain) < 0.05 and peak <= -1.5:
        return source, {"inputLUFS": integrated, "outputLUFS": integrated, "truePeakDBTP": peak, "gainDB": 0}
    if video:
        duration, media = probe(source, "video")
        audio = [s for s in media["streams"] if s["codec_type"] == "audio"]
        if len(audio) != 1:
            raise ValueError("Video balancing requires one audio stream. Select the intended version first.")
        args = ["-map", "0", "-c", "copy", "-c:a:0", "aac", "-b:a:0", "256k",
                "-filter:a:0", f"volume={gain:.6f}dB"]
    else:
        duration, _ = probe(source, "audio")
        args = ["-map", "0:a:0", "-map_metadata", "0", "-c:a", "alac", "-af", f"volume={gain:.6f}dB"]
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


def read_page_rows(page, words, page_number, parse_time):
    for line in row_lines(words):
        headers = {word["text"].upper(): word for word in line}
        if "MUSIC" not in headers or "EXERCISE" not in headers:
            continue
        if not any(h in headers for h in ("CTS", "REPS", "LEVEL")):
            continue
        def leading(key):
            return headers[key]["x0"] - 4 if key in headers else page.width
        music, exercise = leading("MUSIC"), leading("EXERCISE")
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
            def column(begin, end):
                return "\n".join(" ".join(w["text"] for w in line)
                                 for line in row_lines([w for w in body if begin <= w["x0"] < end]))
            action = column(exercise, min((x for x in (counts, reps, level) if x > exercise), default=page.width))
            if action:
                cue = {"time": time, "page": page_number, "action": action,
                       "music": column(music, min(exercise, counts)),
                       "counts": column(counts, exercise if counts < exercise else min(reps, level)),
                       "repetitions": column(reps, level)}
                if color := visible_level(page, level + 6, top + 3):
                    cue["level"] = color
                rows.append(cue)
        return rows
    return []


def extract_cues(path, songs, ocr=False):
    import pdfplumber
    alignment = alignment_module()
    with pdfplumber.open(path) as pdf:
        words = {}
        found = []
        for index, page in enumerate(pdf.pages, 1):
            positioned = page.extract_words()
            if not positioned and ocr:
                positioned = ocr_words(path, index, page.width)
            words[index] = positioned
            if parsed := alignment.parse_cue_page_words(index, page.width, positioned):
                found.append(parsed)
        timelines, warnings = alignment._build_pdf_timelines(found)
        result = []
        for song in songs:
            section = re.fullmatch(r"T(\d+)([A-Z]?)", song["section"])
            candidates = [t for t in timelines if t.track_number == int(section[1])
                          and (t.segment or "") == section[2] and t.variant == song["variant"]]
            pages = song.get("pdfPages", candidates[0].pages if len(candidates) == 1 else [])
            rows = []
            for n in pages:
                if type(n) is not int or n not in words:
                    raise ValueError(f"Invalid PDF page {n} for {song['section']}")
                rows.extend(read_page_rows(pdf.pages[n - 1], words[n], n, alignment.parse_timestamp))
            valid = all(0 <= row["time"] <= song["duration"] for row in rows)
            valid = valid and all(a["time"] <= b["time"] for a, b in zip(rows, rows[1:]))
            result.append({"rows": rows, "pages": list(pages), "approved": False,
                           "status": "review" if valid and rows else "unresolved"})
        return len(pdf.pages), result, warnings
