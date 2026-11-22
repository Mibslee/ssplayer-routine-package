#!/usr/bin/env python3
"""Generate nine offline evidence cards from a verified .ssroutine file."""

from __future__ import annotations

import argparse
from datetime import datetime
import hashlib
from io import BytesIO
import json
from pathlib import Path
import subprocess
import tempfile

import numpy as np
import pypdfium2
from PIL import Image, ImageDraw, ImageFont, ImageOps

from media_analysis import measure, run
from ssroutine import MAX_PACKAGE, consume, digest, read_manifest, require, validate_manifest

SIZE = (1200, 810)
BACKGROUND = "#0b1521"
PANEL = "#162638"
PRIMARY = "#f4f6fb"
SECONDARY = "#b6c5d5"
ACCENT = "#75efc0"
WARNING = "#ffcc93"
FONT_PATHS = [
    "/System/Library/Fonts/STHeiti Medium.ttc",
    "/System/Library/Fonts/STHeiti Light.ttc",
    "C:/Windows/Fonts/msyh.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
]


def source_files(package, work):
    before = package.stat()
    require(before.st_size <= MAX_PACKAGE, "Package too large")
    with package.open("rb") as stream:
        encoded, manifest = read_manifest(stream)
        paths = []
        for index, asset in enumerate(validate_manifest(manifest)):
            path = work / f"{index}.{asset['ext']}"
            with path.open("xb") as destination:
                consume(stream, asset, destination=destination)
            paths.append(path)
        require(not stream.read(1), "Trailing data in package")
    full_sha = digest(package)
    after = package.stat()
    require((before.st_ino, before.st_size, before.st_mtime_ns) ==
            (after.st_ino, after.st_size, after.st_mtime_ns), "Package changed during inspection")
    music_count = len(manifest["music"])
    others = iter(paths[music_count:])
    optional = {key: next(others) for key in ("video", "pdf", "cover") if manifest.get(key)}
    return manifest, paths[:music_count], optional, full_sha, hashlib.sha256(encoded).hexdigest()


def decoded_window(path, start, seconds):
    data = run("ffmpeg", "-nostdin", "-v", "error", "-ss", f"{start:.6f}", "-i", str(path),
               "-t", f"{seconds:.6f}", "-map", "0:a:0", "-ac", "1", "-ar", "4000", "-af",
               "highpass=f=170,lowpass=f=1650", "-f", "f32le", "-")
    return np.frombuffer(data, dtype="<f4").astype(np.float64)


def timing_windows(song, audio, video):
    chapter = song.get("chapter")
    if not chapter or chapter["evidence"] != "audioMatched":
        return {"status": chapter["evidence"] if chapter else "unlocated", "windows": []}
    start, length = chapter["start"], song["duration"]
    width = min(5.0, length * 0.16)
    margin = 0.36
    windows = []
    for part in (0.14, 0.48, 0.80):
        inside = length * part
        actual_margin = min(margin, start + inside)
        a = decoded_window(audio, inside, width)
        b = decoded_window(video, start + inside - actual_margin, width + 2 * actual_margin)
        if len(a) < 600 or len(b) < len(a):
            continue
        a -= a.mean()
        products = np.correlate(b, a, "valid")
        energy = np.insert(np.cumsum(b * b), 0, 0.0)
        denominator = np.sqrt(np.maximum(0, np.dot(a, a) * (energy[len(a):] - energy[:-len(a)])))
        scores = np.divide(products, denominator, out=np.zeros_like(products), where=denominator > 0)
        peak = int(np.argmax(scores))
        windows.append({"position": part, "offsetMS": round((peak / 4000 - actual_margin) * 1000, 1),
                        "correlation": round(float(scores[peak]), 4)})
    offsets = [window["offsetMS"] for window in windows]
    matches = (len(windows) == 3 and max(abs(value) for value in offsets) <= 150
               and max(offsets) - min(offsets) <= 120
               and min(window["correlation"] for window in windows) >= 0.10
               and max(window["correlation"] for window in windows) >= 0.22)
    return {"status": "measured" if matches else "review", "windows": windows}


def inspect_tracks(manifest, paths, optional):
    result = []
    for song, path in zip(manifest["music"], paths):
        measured = {}
        try:
            loudness, peak = measure(path)
            measured.update(lufs=loudness, truePeakDBTP=peak, decoded=True)
        except (ValueError, OSError, subprocess.SubprocessError):
            measured.update(decoded=False)
        chapter = song.get("chapter")
        try:
            timing = timing_windows(song, path, optional["video"]) if chapter else {"status": "unlocated", "windows": []}
        except (ValueError, OSError):
            timing = {"status": "review", "windows": []}
        rows = len(song.get("cues", []))
        result.append({"section": song["section"], "variant": song["variant"], "title": song.get("title"),
                       "duration": song["duration"], "pdfPage": song.get("pdfPage")
                       or (song["cues"][0]["page"] if rows else None), "cueRows": rows,
                       "chapter": chapter, **measured, **timing})
    return result


class Card:
    def __init__(self, header, footer, font_path):
        self.image = Image.new("RGB", SIZE, BACKGROUND)
        self.draw = ImageDraw.Draw(self.image)
        self.font_path = font_path
        self.text(48, 36, "SSplayer   /   离线资源包质检", 25, ACCENT)
        self.text(48, 83, header, 51)
        self.draw.line((48, 153, 1152, 153), fill=PANEL, width=3)
        self.text(48, 758, footer, 22, SECONDARY)

    def font(self, size):
        return ImageFont.truetype(self.font_path, size) if self.font_path else ImageFont.load_default(size=size)

    def text(self, x, y, value, size=31, color=PRIMARY, limit=1104):
        value = " ".join(str(value).split())
        font = self.font(size)
        while len(value) > 1 and self.draw.textlength(value, font=font) > limit:
            value = value[:-2].rstrip() + "…"
        self.draw.text((x, y), value, font=font, fill=color)

    def rows(self, values):
        columns = 2 if len(values) > 10 else 1
        width = 520 if columns == 2 else 1070
        visible = values[:20]
        for index, (label, detail) in enumerate(visible):
            column, row = divmod(index, 10)
            x, y = 48 + column * 552, 180 + row * 51
            self.draw.rounded_rectangle((x, y, x + width, y + 43), 8, fill=PANEL)
            self.text(x + 13, y + 7, label, 23, ACCENT, 170)
            self.text(x + 177, y + 7, detail, 23, PRIMARY, width - 195)
        if len(values) > len(visible):
            self.text(50, 701, f"另有 {len(values) - len(visible)} 项，详见 qa.json", 24, SECONDARY)

    def media(self, image, box=(48, 180, 1104, 515)):
        if image:
            x, y, width, height = box
            fitted = ImageOps.contain(image.convert("RGB"), (width, height), Image.Resampling.LANCZOS)
            self.image.paste(fitted, (x + (width - fitted.width) // 2, y + (height - fitted.height) // 2))


def clock(seconds):
    whole = int(seconds)
    return f"{whole // 60}:{whole % 60:02d}"


def opening_preview(path, page):
    with pypdfium2.PdfDocument(path) as document:
        require(1 <= page <= len(document), "Opening page out of range")
        with document[page - 1] as source:
            return source.render(scale=1.5).to_pil().copy()


def screenshot_cards(manifest, files, report, font_path):
    tracks = report["tracks"]
    art = f"{manifest['program']} {manifest['routine']}"
    chapter_set = [entry for entry in tracks if entry["chapter"]]
    measured = sum(entry["status"] == "measured" for entry in tracks)
    with_pages = [entry for entry in tracks if entry["pdfPage"]]
    cue_tracks = [song for song in manifest["music"] if song.get("cues")]
    with_cues = sum(entry["cueRows"] for entry in tracks)
    marker = report["sha256"][:12]

    def card(number, title):
        return Card(title, f"{number:02d} / 09     {art}     SHA-256 {marker}", font_path)

    cover = card(1, art)
    if "cover" in files:
        with Image.open(files["cover"]) as source:
            cover.media(source, (676, 181, 466, 500))
    for i, (label, value) in enumerate([
        ("制作者", report["credit"] or "未署名"), ("项目", manifest["program"]),
        ("套路号", manifest["routine"]), ("版本号", manifest["revision"]),
        ("发行时间", manifest.get("releaseDate", "未核对")),
        (report["madeAtLabel"], report["madeAt"][:19].replace("T", " ")),
    ]):
        cover.text(48, 187 + 80 * i, label, 23, ACCENT)
        cover.text(48, 216 + 80 * i, value, 34, limit=595)
    yield "01-identity.png", cover.image

    counts = card(2, "包内资料")
    counts.rows([("音乐", f"{len(tracks)} 首"), ("视频", "已包含" if "video" in files else "缺失"),
                 ("原谱", f"{manifest['pdf']['pageCount']} 页" if "pdf" in files else "缺失"),
                 ("封面", "已包含" if "cover" in files else "App 会用占位图"),
                 ("视频定位", f"{len(chapter_set)} 个 · 实测吻合 {measured} 个"),
                 ("原谱入口", f"{len(with_pages)} 个物理页入口"), ("动作", f"{with_cues} 条"),
                 ("完整性", f"{report['bytes']:,} 字节 · 所有载荷 SHA-256 已核对")])
    yield "02-inventory.png", counts.image

    names = {"standard": "正曲", "alternative": "替换", "bonus": "加曲"}
    music = card(3, "曲目、替换与分段")
    music.rows([(f"{row['section']}  {names.get(row['variant'], row['variant'])}",
                 f"{clock(row['duration'])}   {row['title'] or row['section']}") for row in tracks])
    yield "03-track-list.png", music.image

    video = card(4, "视频画面与定位")
    if chapter_set and "video" in files:
        entry = next((row for row in chapter_set if row["status"] == "measured"), chapter_set[0])
        chapter = entry["chapter"]
        point = chapter["start"] + min(12, (chapter["end"] - chapter["start"]) / 2)
        try:
            image = run("ffmpeg", "-nostdin", "-v", "error", "-ss", str(point), "-i", str(files["video"]),
                        "-map", "0:v:0", "-frames:v", "1", "-vf", "scale=880:-2", "-f", "image2pipe", "-vcodec", "png", "-")
            with Image.open(BytesIO(image)) as frame:
                video.media(frame, (48, 181, 1104, 478))
            video.text(48, 680, f"{entry['section']}    画面取自 {clock(point)}    章 {clock(chapter['start'])}–{clock(chapter['end'])}", 27)
        except (ValueError, OSError):
            video.text(48, 185, "视频帧解码需复查", 34, WARNING)
    else:
        video.text(48, 185, "包中无已声明的视频定位")
    yield "04-video-seek.png", video.image

    timing = card(5, "音乐与视频时钟复测")
    timing.text(48, 700, "前／中／尾三个窗口   •   数值为相对包内定位偏移（ms）", 23, SECONDARY)
    states = {"measured": "吻合", "review": "需复核", "unlocated": "无定位",
              "approximate": "近似", "visuallyConfirmed": "按画面"}
    timing.rows([(row["section"], states[row["status"]] + "    " +
                 " / ".join(f"{point['offsetMS']:+.0f}" for point in row["windows"])) for row in tracks])
    yield "05-timing.png", timing.image

    cues = card(6, "动作时间线")
    if cue_tracks:
        song = max(cue_tracks, key=lambda entry: len(entry["cues"]))
        cues.rows([(f"{song['section']}  {clock(cue['time'])}", cue["action"]) for cue in song["cues"]])
    else:
        cues.rows([("原谱入口", f"{len(with_pages)} 处"), ("动作时钟", "本包未提交可跳转动作点"),
                   ("阅读方式", "打开该小节的原谱，查看原始动作和持续时长")])
    yield "06-action-timeline.png", cues.image

    pdf = card(7, "原谱打开页")
    if "pdf" in files:
        selection = []
        for index in (0, len(with_pages) // 2, len(with_pages) - 1):
            if with_pages and with_pages[index] not in selection:
                selection.append(with_pages[index])
        if not selection:
            selection = [{"section": "原谱", "pdfPage": 1}]
        for index, row in enumerate(selection):
            x = 48 + index * 365
            pdf.media(opening_preview(files["pdf"], row["pdfPage"]), (x, 184, 342, 480))
            pdf.text(x, 679, f"{row['section']}    物理页 {row['pdfPage']}", 25, limit=340)
    else:
        pdf.text(48, 185, "包中没有原谱")
    yield "07-source-pdf.png", pdf.image

    volume = card(8, "声音解码与峰值")
    volume.rows([(row["section"], f"{row['lufs']:.2f} LUFS    {row['truePeakDBTP']:.2f} dBTP"
                  if row["decoded"] else "解码或测量需复核") for row in tracks])
    yield "08-loudness.png", volume.image

    gaps = card(9, "复核清单")
    issues = [(row["section"], " / ".join([reason for condition, reason in [
        (not row["decoded"], "音乐解码"), (row["status"] == "review", "对位"),
        (row["status"] == "unlocated" and "video" in files, "无视频定位")] if condition]))
              for row in tracks]
    gaps.rows([("App", "离线质检已生成，实际导入与排课需在 App 核对"),
               ("署名", "随本次报告显示"), ("完整摘要", marker + "… 见 qa.json"),
               *(("编号间隔", f"T{number} · 对照原谱确认") for number in report["sectionGaps"]),
               *(item for item in issues if item[1])])
    yield "09-review.png", gaps.image


def generate(args):
    root = args.output_dir
    root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="ssroutine-qa-") as staging, \
         tempfile.TemporaryDirectory(dir=root, prefix=".ssroutine-report-") as report_staging:
        work = Path(staging)
        manifest, audio, optional, full_sha, manifest_sha = source_files(args.package, work)
        name = f"{manifest['program']}_{manifest['routine']}_{manifest['revision']}.qa"
        destination = root / name
        require(not destination.exists(), f"Evidence directory already exists: {name}")
        credit = args.credit if args.credit is not None else manifest.get("credit")
        require(credit is None or 0 < len(credit.encode("utf-8")) <= 128, "Credit is limited to 128 bytes")
        date = datetime.fromisoformat(args.made_at) if args.made_at else datetime.now().astimezone()
        font_path = str(args.font) if args.font else next((file for file in FONT_PATHS if Path(file).is_file()), None)
        sections = {int(song["section"][1:].rstrip("ABCDEFGHIJKLMNOPQRSTUVWXYZ")) for song in manifest["music"]}
        report = {"schema": "ssroutine.offline.qa.v1", "program": manifest["program"],
                  "routine": manifest["routine"], "revision": manifest["revision"], "credit": credit,
                  "releaseDate": manifest.get("releaseDate"), "madeAt": date.isoformat(timespec="seconds"),
                  "madeAtLabel": "制作时间" if args.made_at else "质检时间",
                  "bytes": args.package.stat().st_size, "sha256": full_sha, "manifestSHA256": manifest_sha,
                  "sectionGaps": sorted(set(range(1, max(sections) + 1)) - sections),
                  "appImportVerified": False, "tracks": inspect_tracks(manifest, audio, optional)}
        output = Path(report_staging) / "evidence"
        output.mkdir()
        contact = Image.new("RGB", (1832, 1250), BACKGROUND)
        for index, (file, frame) in enumerate(screenshot_cards(manifest, optional, report, font_path)):
            frame.save(output / file)
            contact.paste(ImageOps.contain(frame, (596, 403), Image.Resampling.LANCZOS),
                          (14 + (index % 3) * 605, 14 + (index // 3) * 407))
        contact.save(output / "3x3-overview.png")
        (output / "qa.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        output.rename(destination)
    print(json.dumps({"output": str(destination.resolve()), "screenshots": 9, "sha256": full_sha,
                      "measuredChapters": sum(row["status"] == "measured" for row in report["tracks"])}))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("package", type=Path)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--credit", help="Display credit for this evidence set")
    parser.add_argument("--made-at", help="Verified creation time in ISO 8601; omit to use evidence generation time")
    parser.add_argument("--font", type=Path, help="Optional font with Chinese glyphs")
    try:
        generate(parser.parse_args())
    except (ValueError, OSError, TypeError, KeyError) as error:
        parser.exit(1, f"{error}\n")


if __name__ == "__main__":
    main()
