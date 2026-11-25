"""Inspect packaged subtitle streams and sample the finished video for burned-in text."""

from collections import defaultdict
import csv
from io import BytesIO, StringIO
from pathlib import Path
import shutil
import subprocess
import unicodedata

from PIL import Image, ImageDraw, ImageOps

from media_analysis import map_recordings, run

SAMPLE_COUNT = 24
PREFERRED_LANGUAGES = ("eng", "chi_sim", "chi_tra", "jpn", "kor", "spa", "por", "fra", "deu")


def read_frame(path, seconds, width):
    raw = run("ffmpeg", "-nostdin", "-v", "error", "-ss", f"{seconds:.6f}", "-i", str(path),
              "-map", "0:V:0", "-frames:v", "1", "-vf", f"scale={width}:-2",
              "-f", "image2pipe", "-vcodec", "mjpeg", "-")
    with Image.open(BytesIO(raw)) as source:
        return source.convert("RGB")


def ocr_languages(executable):
    if not executable:
        return ()
    installed = set(run(executable, "--list-langs").decode(errors="replace").splitlines())
    return tuple(language for language in PREFERRED_LANGUAGES if language in installed)


def caption_text(executable, languages, frame):
    width, height = frame.size
    # Use the centered lower subtitle area. Keep full frames for visual review so
    # captions elsewhere and text in languages absent from OCR can be checked.
    region = frame.crop((width // 8, int(height * .60), width * 7 // 8, int(height * .98)))
    region = ImageOps.autocontrast(ImageOps.grayscale(region))
    region = region.resize((region.width * 2, region.height * 2), Image.Resampling.LANCZOS)
    data = BytesIO()
    region.save(data, format="PNG")
    output = subprocess.run((executable, "stdin", "stdout", "-l", "+".join(languages),
                             "--psm", "11", "tsv"), input=data.getvalue(), capture_output=True,
                            check=True, timeout=15).stdout.decode(errors="replace")
    lines = defaultdict(list)
    for row in csv.DictReader(StringIO(output), delimiter="\t"):
        if row.get("level") != "5" or float(row["conf"]) < 68:
            continue
        lines[(row["block_num"], row["par_num"], row["line_num"])].append(row["text"].strip())
    detected = []
    for words in lines.values():
        sentence = " ".join(words).strip()
        letters = [letter for letter in sentence if letter.isalpha()]
        cjk = sum(unicodedata.east_asian_width(letter) in {"W", "F"} for letter in letters)
        if cjk >= 5 or (len(letters) >= 12 and len(words) >= 3):
            detected.append(sentence[:120])
    return detected


def write_samples(samples, destination):
    cells = (384, 248)
    sheet = Image.new("RGB", (cells[0] * 4, cells[1] * ((len(samples) + 3) // 4)), "#0b1521")
    draw = ImageDraw.Draw(sheet)
    for index, (seconds, frame, _) in enumerate(samples):
        x, y = (index % 4) * cells[0], (index // 4) * cells[1]
        fitted = ImageOps.contain(frame, (cells[0] - 12, cells[1] - 28))
        sheet.paste(fitted, (x + (cells[0] - fitted.width) // 2, y + 6))
        minute, second = divmod(int(seconds), 60)
        draw.text((x + 12, y + cells[1] - 23), f"{minute:02d}:{second:02d}", fill="#f4f6fb")
    sheet.save(destination, format="JPEG", quality=92)


def inspect_screen_subtitles(path, duration, sample_file=None, observation=None):
    executable = shutil.which("tesseract")
    try:
        languages = ocr_languages(executable) if observation is None else ()
    except (OSError, ValueError):
        languages = ()
    # Midpoints span the final file, including intros and unlocated tracks.
    positions = [duration * (index + .5) / SAMPLE_COUNT for index in range(SAMPLE_COUNT)]

    def sample(seconds):
        try:
            frame = read_frame(path, seconds, 768)
        except (ValueError, OSError):
            return None
        text = None
        if languages:
            try:
                text = caption_text(executable, languages, frame)
            except (ValueError, OSError, subprocess.SubprocessError):
                pass
        return seconds, frame, text

    samples = [item for item in map_recordings(sample, positions) if item is not None]
    if samples and sample_file:
        write_samples(samples, Path(sample_file))
    reviewed = [text for _, _, text in samples if text is not None]
    unique_lines = {line.casefold() for frame_lines in reviewed for line in frame_lines}
    if observation and samples:
        result = "present" if observation == "present" else "notDetected"
        method = "visual"
    elif len(unique_lines) >= 3 and sum(bool(lines) for lines in reviewed) >= 3:
        # Several different sentence-like lines in the subtitle region are
        # stronger evidence than an isolated title or fixed channel label.
        result, method = "present", "ocr"
    elif unique_lines:
        result, method = "candidate", "ocr"
    elif len(reviewed) == SAMPLE_COUNT:
        result, method = "notDetected", "ocr"
    else:
        result, method = "review", "ocr" if languages else "visual"
    return {"result": result, "method": method, "frames": len(samples), "ocrFrames": len(reviewed),
            "languages": list(languages), "candidateFrames": sum(bool(lines) for lines in reviewed)}


def subtitle_summary(picture, screen):
    parts = [f"有独立字幕轨 · {picture['subtitleTracks']} 条" if picture["subtitleTracks"] else "无独立字幕轨"]
    if picture["closedCaptions"]:
        parts.append("有闭路字幕")
    frames = screen["frames"]
    description = {
        "present": f"有画面字幕 · 已看 {frames} 帧" if screen["method"] == "visual"
                   else f"有画面字幕 · OCR 抽查 {frames} 帧",
        "notDetected": f"抽查 {frames} 帧，未检出画面字幕" if screen["method"] == "visual"
                       else f"下方 OCR 抽查 {frames} 帧未检出字幕",
        "candidate": f"抽查 {frames} 帧发现疑似字幕",
        "review": f"抽查 {frames} 帧，画面字幕需确认" if frames else "无法抽查画面字幕",
    }[screen["result"]]
    return " · ".join(parts), description
