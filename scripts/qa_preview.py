#!/usr/bin/env python3
"""Generate nine offline evidence cards from a verified .ssroutine file."""

from __future__ import annotations

import argparse
from bisect import bisect_left
from contextlib import ExitStack
from datetime import datetime
import hashlib
import json
from pathlib import Path
import re
from statistics import median
import subprocess
import struct
import tempfile

import pypdfium2
from PIL import Image, ImageDraw, ImageFont, ImageOps

from delivery_policy import assess_delivery
from grit_evidence import check_grit_section_function, grit_source_evidence
from grit_intervals import validate_interval_review
from media_analysis import (SAMPLE_POSITIONS, alignment_module, extract_cues, map_recordings, measure,
                            probe, timing_failure_detail, timing_windows, video_info)
from qa_phone import (FONT_PATHS, PhonePreview, clock, score_metadata, section_title, select_preview_tracks,
                      timeline_sample, video_position, wrapped_lines)
from report_html import write_html
from ssroutine import (MAGIC, MAX_PACKAGE, check_class_template_sources, consume, container_digest,
                       grit_interval_proposals, read_class_template_document, read_manifest, require, validate_manifest)
from subtitle_check import inspect_screen_subtitles, read_frame, subtitle_summary

SIZE = (1200, 810)
BACKGROUND = "#0b1521"
PANEL = "#162638"
PRIMARY = "#f4f6fb"
SECONDARY = "#b6c5d5"
ACCENT = "#75efc0"
WARNING = "#ffcc93"
STYLES = {
    "passed": ("通过", ACCENT, "#183833"),
    "conditional": ("资料受限", WARNING, "#3c2e24"),
    "failed": ("暂缓交付", "#ffa5a8", "#412730"),
}
CHECK_LABELS = {"contents": "包内资料", "audio": "音乐", "video": "视频定位", "subtitles": "字幕",
                "timeline": "动作时间线", "pdf": "原版波形图", "structure": "小节功能", "formats": "课型识别"}
TIMING_STATES = {"measured": "音画复测吻合", "review": "音画对位需复核", "unlocated": "未指定小节定位",
                 "approximate": "近似定位", "visuallyConfirmed": "按画面定位"}
def add_issue(report, check, detail):
    report["checks"][check]["issues"].append(detail)
    report["blockingIssues"].append(detail)


def add_condition(report, check, detail):
    report["checks"][check]["conditions"].append(detail)


def add_source_limitation(report, check, code, detail):
    add_condition(report, check, detail)
    report.setdefault("sourceLimitations", []).append({"check": check, "code": code, "detail": detail})


def delivery_display(manifest, report, *keys):
    selected = {key: report["checks"][key] for key in keys} if keys else report["checks"]
    outcome = assess_delivery(manifest, {**report, "checks": selected},
                              source_limited=report.get("sourceLimited", False))
    state = {"ready": "passed", "readyWithLimitations": "conditional", "blocked": "failed"}[outcome["status"]]
    if state == "conditional" and all(item["code"] == "supplementary_outro" for item in outcome["limitations"]):
        state = "passed"
    return state, outcome


def inspect_loudness(report, target, *, keep_music=False):
    tracks = [row for row in report["tracks"] if row["decoded"] and row["durationMatches"]]
    if keep_music:
        reference = None
        report["loudnessPolicy"] = "keepOriginalDynamics"
    elif target is not None:
        reference = target
        label = "制作目标"
    else:
        reference = median(row["lufs"] for row in tracks) if len(tracks) >= 3 else None
        label = "包内中位响度"
    if reference is not None:
        report["loudnessReference"] = {
            "lufs": reference, "source": "target" if target is not None else "packageMedian",
        }
    for row in tracks:
        title = section_title(row)
        if reference is not None and abs(delta := round(row["lufs"] - reference, 2)) > 2.0:
            add_condition(report, "audio",
                          f"{title} 实测 {row['lufs']:.1f} LUFS，较{label} {reference:.1f} LUFS 偏差 {delta:+.1f} LU。")
            row["loudnessReview"] = True
        if row["truePeakDBTP"] > 0:
            add_issue(report, "audio",
                      f"{title} 实测真峰值 {row['truePeakDBTP']:+.1f} dBTP，高于 0 dBTP。")
            row["loudnessReview"] = True


def check_state(report, *keys):
    checks = [report["checks"][key] for key in keys] if keys else report["checks"].values()
    if any(check["issues"] for check in checks):
        return "failed"
    if any(check["conditions"] for check in checks):
        return "conditional"
    return "passed"


def sections_for(tracks):
    return "、".join(dict.fromkeys(section_title(track) for track in tracks))


def missing_printed_cue_times(source_rows, packaged_rows):
    recorded = sorted(cue["time"] for cue in packaged_rows)
    missed = []
    for time in sorted({cue["time"] for cue in source_rows}):
        index = bisect_left(recorded, time - 0.5)
        if index == len(recorded) or recorded[index] > time + 0.5:
            missed.append(time)
    return missed


def readable_untimed_score(pdf, tracks):
    """Corroborate an explicitly limited course against its readable original."""
    import pdfplumber

    opening_pages = {track["pdfPage"] for track in tracks}
    if not opening_pages or None in opening_pages:
        return False
    clock = re.compile(r"\b\d{1,3}\s*[:：.]\s*[0-5]\d\b"
                       r"|\b\d+\s*(?:secs?(?:onds?)?|mins?(?:utes?)?)\b"
                       r"|\d+\s*(?:分钟|秒钟|分\s*\d|秒)"
                       r"|\bDURATION\b|时长", re.I)
    with pdfplumber.open(pdf) as document:
        for page_number, page in enumerate(document.pages, 1):
            content = (page.extract_text() or "").strip()
            if (not content and (page_number != 1 or page_number in opening_pages)) or clock.search(content):
                return False
    return True


def class_slot_label(slot, *, show_edition=False):
    codes = slot.get("sectionCodes") or [f"T{number}" for number in slot["sectionNumbers"]]
    label = ("+" if slot.get("selectionMode") == "consecutive" else "/").join(codes)
    if show_edition and (edition := slot.get("requiredDurationMinutes")):
        label += f"（{edition} 分钟版）"
    return f"({label})" if slot.get("isOptional") else label


def class_slot_recordings(slot, songs):
    edition = slot.get("requiredDurationMinutes")
    codes = slot.get("sectionCodes")
    matches = {}
    for index, song in enumerate(songs):
        code = song["section"]
        if int(code[1:].rstrip("ABCDEFGHIJKLMNOPQRSTUVWXYZ")) not in slot["sectionNumbers"] or (codes and code not in codes):
            continue
        if edition is not None and song.get("classDurationMinutes") != edition:
            continue
        preference = slot.get("variantPreference")
        if slot.get("enforcesVariant") and preference in ("standard", "alternative"):
            if (song["variant"] == "standard") != (preference == "standard"):
                continue
        matches.setdefault(code, []).append(index)
    if codes and slot.get("selectionMode") == "consecutive":
        return [matches.get(code, []) for code in codes]
    return [[index for by_code in matches.values() for index in by_code]]


def class_recording_conditions(template, songs):
    missing, demands = [], []
    for slot in template["slots"]:
        if slot.get("isOptional"):
            continue
        candidates = class_slot_recordings(slot, songs)
        if all(candidates):
            demands.extend(candidates)
        else:
            label = class_slot_label(slot, show_edition=True)
            missing.append(label)
    if missing:
        return [f"{template['name']} · 缺 {'、'.join(missing)} 的录音。"]

    # Each position can use one recording. Relocate earlier choices to avoid
    # reporting a conflict when a different valid recording is available.
    assigned = {}

    def assign(position, visited):
        for track in demands[position]:
            if track in visited:
                continue
            visited.add(track)
            if track not in assigned or assign(assigned[track], visited):
                assigned[track] = position
                return True
        return False

    if all(assign(position, set()) for position in range(len(demands))):
        return []
    return [f"{template['name']} · 包内不同录音不足以填满所有必选位置，可从其他套路补选。"]


def source_files(package, work, sealed_copy=None):
    before = package.stat()
    require(before.st_size <= MAX_PACKAGE, "Package too large")
    with ExitStack() as stack:
        stream = stack.enter_context(package.open("rb"))
        encoded, manifest = read_manifest(stream)
        mirror = stack.enter_context(sealed_copy.open("xb")) if sealed_copy else None
        if mirror:
            mirror.write(MAGIC + struct.pack(">I", len(encoded)) + encoded)
        full_digest = container_digest(encoded)
        paths = []
        for index, asset in enumerate(validate_manifest(manifest)):
            path = work / f"{index}.{asset['ext']}"
            with path.open("xb") as destination:
                consume(stream, asset, destination=destination, package_digest=full_digest, mirror=mirror)
            paths.append(path)
        require(not stream.read(1), "Trailing data in package")
    after = package.stat()
    require((before.st_ino, before.st_size, before.st_mtime_ns) ==
            (after.st_ino, after.st_size, after.st_mtime_ns), "Package changed during inspection")
    music_count = len(manifest["music"])
    others = iter(paths[music_count:])
    optional = {key: next(others) for key in ("video", "pdf", "cover", "classTemplatesFile") if manifest.get(key)}
    return (manifest, paths[:music_count], optional, after.st_size,
            full_digest.hexdigest(), hashlib.sha256(encoded).hexdigest())


def inspect_tracks(manifest, paths, optional):
    alignment = alignment_module() if optional.get("video") else None

    def inspect(item):
        song, path = item
        measured = {}
        try:
            loudness, peak = measure(path)
            duration, _ = probe(path, "audio")
            measured.update(lufs=loudness, truePeakDBTP=peak, decoded=True, measuredDuration=duration,
                            durationMatches=abs(duration - song["duration"]) <= 0.25)
        except (ValueError, OSError, subprocess.SubprocessError):
            measured.update(decoded=False)
        chapter = song.get("chapter")
        try:
            timing = timing_windows(song, path, optional["video"], alignment) if chapter else {"status": "unlocated", "windows": []}
        except (ValueError, OSError):
            timing = {"status": "review", "windows": []}
        rows = len(song.get("cues") or [])
        return {"section": song["section"], "variant": song["variant"], "title": song.get("title"),
                "duration": song["duration"], "pdfPage": song.get("pdfPage")
                or (song["cues"][0]["page"] if rows else None), "cueRows": rows,
                "sectionFunction": song.get("sectionFunction"), "chapter": chapter, **measured, **timing}

    return map_recordings(inspect, zip(manifest["music"], paths))


class Card:
    def __init__(self, header, footer, font_path):
        self.image = Image.new("RGB", SIZE, BACKGROUND)
        self.draw = ImageDraw.Draw(self.image)
        self.font_path = font_path
        if header:
            self.text(48, 36, "SSplayer   /   离线资源包质检", 25, ACCENT)
            self.text(48, 83, header, 51)
            self.draw.line((48, 153, 1152, 153), fill=PANEL, width=3)
        if footer:
            self.text(48, 758, footer, 22, SECONDARY)

    def font(self, size):
        return ImageFont.truetype(self.font_path, size) if self.font_path else ImageFont.load_default(size=size)

    def text(self, x, y, value, size=31, color=PRIMARY, limit=1104):
        value = " ".join(str(value).split())
        font = self.font(size)
        while len(value) > 1 and self.draw.textlength(value, font=font) > limit:
            value = value[:-2].rstrip() + "…"
        self.draw.text((x, y), value, font=font, fill=color)

    def status(self, state, y, x=None, marker_only=False):
        name, color, background = STYLES[state]
        width = 58 if marker_only else int(self.draw.textlength(name, font=self.font(24))) + 62
        x = 1152 - width if x is None else x
        self.draw.rounded_rectangle((x, y, x + width, y + 43), 10, fill=background)
        left, top = x + 15, y + 10
        if state == "passed":
            self.draw.line(((left, top + 12), (left + 7, top + 19), (left + 21, top + 4)), fill=color, width=4)
        elif state == "conditional":
            self.draw.line(((left + 10, top + 2), (left + 10, top + 12)), fill=color, width=4)
            self.draw.ellipse((left + 8, top + 16, left + 12, top + 20), fill=color)
        else:
            self.draw.line(((left + 3, top + 4), (left + 19, top + 20)), fill=color, width=4)
            self.draw.line(((left + 19, top + 4), (left + 3, top + 20)), fill=color, width=4)
        if not marker_only:
            self.text(x + 43, y + 7, name, 24, color)

    def rows(self, values, *, top=180):
        columns = 2 if len(values) > 10 else 1
        width = 520 if columns == 2 else 1070
        visible = values[:20]
        for index, (label, detail) in enumerate(visible):
            column, row = divmod(index, 10)
            x, y = 48 + column * 552, top + row * 51
            self.draw.rounded_rectangle((x, y, x + width, y + 43), 8, fill=PANEL)
            self.text(x + 13, y + 7, label, 23, ACCENT, 170)
            self.text(x + 177, y + 7, detail, 23, PRIMARY, width - 195)
        if len(values) > len(visible):
            self.text(50, top + 521, f"另有 {len(values) - len(visible)} 项，见完整报告", 24, SECONDARY)

    def media(self, image, box=(48, 180, 1104, 515)):
        if image:
            x, y, width, height = box
            fitted = ImageOps.contain(image.convert("RGB"), (width, height), Image.Resampling.LANCZOS)
            self.image.paste(fitted, (x + (width - fitted.width) // 2, y + (height - fitted.height) // 2))


def file_size(size):
    for unit, scale in (("GB", 1_000_000_000), ("MB", 1_000_000), ("KB", 1_000)):
        if size >= scale:
            number = f"{size / scale:.2f}".rstrip("0").rstrip(".")
            return f"{number} {unit}"
    return f"{size} B"


def routine_overview(card, title, manifest, files, report):
    card.text(48, 36, "SSplayer   /   离线资源包质检", 25, ACCENT)
    card.draw.line((48, 88, 1152, 88), fill=PANEL, width=2)
    card.draw.rounded_rectangle((48, 117, 428, 551), 18, fill=PANEL)
    cover_status = "未附封面"
    if path := files.get("cover"):
        try:
            with Image.open(path) as source:
                expected = "JPEG" if manifest["cover"]["ext"] == "jpg" else "PNG"
                require(source.format == expected and all(1 <= value <= 8192 for value in source.size),
                        "Cover content, extension or dimensions do not match the App format")
                card.media(ImageOps.exif_transpose(source), (60, 129, 356, 410))
                cover_status = None
        except (ValueError, OSError, Image.DecompressionBombError):
            add_issue(report, "contents", "封面无法解码，或图片格式、尺寸不符合 App 要求。")
            cover_status = "封面需修正"
    if cover_status:
        card.draw.text((238, 334), cover_status, font=card.font(32), anchor="mm",
                       fill=WARNING if "cover" in files else SECONDARY)

    display_name = manifest.get("routineName") or title
    title_width = card.draw.textlength(display_name, font=card.font(78))
    title_size = max(36, min(78, int(78 * 684 / max(title_width, 1))))
    card.text(466, 127, display_name, title_size, limit=686)
    project = manifest.get("projectName")
    if project or display_name != title:
        card.text(466, 202, " · ".join(dict.fromkeys(filter(None, (project, title)))), 25, SECONDARY, limit=686)
    released = report["releaseDate"]
    release_label = f"发行 {released.replace('Q', ' Q')}" if released else "发行时间待核对"
    meta = [release_label, f"包版本 {manifest['revision']}"]
    picture = report.get("videoInfo")
    if picture:
        meta.append("视频 " + clock(picture["duration"]))
    card.text(466, 234 if project or display_name != title else 214, " · ".join(meta), 25, SECONDARY, limit=686)

    card.draw.rounded_rectangle((466, 271, 845, 392), 17, fill=PANEL)
    card.text(488, 286, "资源包大小", 24, SECONDARY)
    card.text(488, 321, file_size(report["bytes"]), 55, limit=335)
    state, outcome = delivery_display(manifest, report)
    status_label, status_color, status_background = STYLES[state]
    card.draw.rounded_rectangle((861, 271, 1152, 392), 17, fill=status_background)
    card.text(885, 286, "交付验收", 24, status_color)
    card.status(state, 327, x=870, marker_only=True)
    card.text(918, 325, "可交付" if state == "passed" else status_label, 39, status_color, limit=223)
    if state == "passed" and outcome["limitations"]:
        card.text(885, 365, "附加收尾曲见说明", 17, status_color, limit=247)

    if picture:
        codec = {"hevc": "H.265", "h264": "H.264"}.get(picture["codec"], picture["codec"].upper())
        video = f"{picture['width']}×{picture['height']} · {codec}"
        details = f"{file_size(picture['bytes'])} · 平均 {picture['bitrateKbps']:,} kb/s"
        subtitles, screen = subtitle_summary(picture, report["screenSubtitles"])
    else:
        video = "视频待核对" if "video" in files else "未附视频"
        details = None
        subtitles = "视频无法读取" if "video" in files else "未附视频，不适用"
        screen = None
    card.text(466, 426, "视频", 26, ACCENT)
    card.text(561, 421, video, 33, limit=591)
    if details:
        card.text(561, 463, details, 25, SECONDARY, limit=591)

    tracks = report["tracks"]
    music = [f"{len(tracks)} 首"]
    if all(row["decoded"] for row in tracks):
        music.append(clock(sum(row["measuredDuration"] for row in tracks)))
    music.append(file_size(sum(song["asset"]["bytes"] for song in manifest["music"])))
    original = "未附原谱"
    if pdf := manifest.get("pdf"):
        pages = report.get("pdfPageCount")
        page_label = f"{pages} 页" if pages else "待核对"
        original = f"{page_label} · {file_size(pdf['asset']['bytes'])}"
    for y, label, value in ((519, "字幕", subtitles), (585, "音乐", " · ".join(music)),
                            (651, "原谱", original)):
        card.draw.line((466, y - 15, 1152, y - 15), fill=PANEL, width=2)
        card.text(466, y + 2, label, 26, ACCENT)
        card.text(561, y, value, 26, limit=591)
        if label == "字幕" and screen:
            card.text(561, y + 28, screen, 22, SECONDARY, limit=591)

    card.text(48, 578, "制作者", 23, SECONDARY)
    card.text(48, 605, report["credit"] or "未署名", 30, limit=380)
    card.text(48, 650, report["madeAtLabel"], 23, SECONDARY)
    card.text(48, 676, report["madeAt"][:19].replace("T", " "), 28, limit=380)
    card.draw.line((48, 729, 1152, 729), fill=PANEL, width=2)


def opening_preview(path, page):
    with pypdfium2.PdfDocument(path) as document:
        require(1 <= page <= len(document), "Opening page out of range")
        source = document[page - 1]
        try:
            bitmap = source.render(scale=1.5)
            try:
                return bitmap.to_pil().copy()
            finally:
                bitmap.close()
        finally:
            source.close()


def measured_offsets(track):
    return {sample["position"]: sample["offsetMS"] for sample in track["windows"]}


def video_seek_card(card, routine, pair, files, report):
    picture = report.get("videoInfo")
    song, track = pair or (None, None)
    position = None
    frame = None
    absent = "包中没有视频" if "video" not in files else "视频信息无法读取"
    if picture:
        position = video_position(song, track, picture["duration"])
        try:
            frame = read_frame(files["video"], position.source_seconds, 880)
        except (ValueError, OSError):
            add_issue(report, "video", "包内视频的抽查帧无法解码。")
            absent = "视频帧解码需复查"
    phone = PhonePreview(card, routine, section_title(song) if song else None, "视频", 0,
                         frame=frame, unavailable_video=None if frame else absent)
    if frame and position:
        if position.uses_music_clock and song.get("cues"):
            phone.timeline(song["cues"], position.local_seconds)
        elif track and track["status"] == "review":
            phone.empty("音画对位需复核", "画面取自包内声明的章节")
        elif not track:
            phone.empty("整课视频", "包内没有指定小节入口")
        elif not position.uses_music_clock:
            phone.empty("按视频章内时间预览")
        else:
            phone.empty("缺少可用动作时间线")
        phone.transport(position.local_seconds, position.local_duration)
    else:
        phone.empty("仍可播放音乐和排课")
        card.text(511, 160, absent, 41)
        return None

    card.text(511, 143, section_title(song) if song else "整课视频", 24, ACCENT)
    title = (song.get("title") or song["section"]) if song else routine
    card.text(511, 181, title, 40, limit=620)
    state = track["status"] if track else "unlocated"
    card.text(511, 247, TIMING_STATES[state], 26, WARNING if state == "review" else SECONDARY)
    label = "小节内时间" if position.uses_music_clock else "视频章内时间" if track else "全片时间"
    for x, title, value in ((511, label, clock(position.local_seconds)),
                            (835, "整课取帧时间", clock(position.source_seconds))):
        card.draw.rounded_rectangle((x, 303, x + 300, 420), 15, fill=PANEL)
        card.text(x + 20, 319, title, 23, SECONDARY)
        card.text(x + 20, 353, value, 42)
    if track:
        chapter = track["chapter"]
        card.text(511, 445, f"整课章节  {clock(chapter['start'])} – {clock(chapter['end'])}", 26)
        windows = measured_offsets(track)
        if state in {"measured", "review"}:
            card.text(511, 516, "音画偏差（毫秒）", 25, SECONDARY)
            positions = sorted(windows) if len(windows) == 3 else [part for part, _ in SAMPLE_POSITIONS]
            for index, (fraction, (_, name)) in enumerate(zip(positions, SAMPLE_POSITIONS)):
                x = 511 + index * 210
                card.draw.line((x, 562, x + 177, 562), fill="#304458", width=2)
                card.text(x, 578, f"{name} {fraction:.0%}", 22, SECONDARY)
                deviation = windows.get(fraction)
                card.text(x, 610, f"{deviation:+.1f}" if deviation is not None else "未测得", 29,
                          ACCENT if state == "measured" else WARNING)
    return position


def action_timeline_card(card, routine, pair, video_pair, position, tracks):
    if not pair:
        phone = PhonePreview(card, routine, None, "音乐", 0)
        phone.empty("本包未附动作时间线")
        card.text(511, 160, "本包未提供动作时间线", 36)
        card.text(511, 215, "音乐仍可按小节播放和排课。", 23, SECONDARY)
        pages = sum(bool(row["pdfPage"]) for row in tracks)
        if pages:
            card.text(511, 256, f"{pages} 首可从「波形图」查看原谱", 27, SECONDARY)
        return None
    song, _ = pair
    rows = song["cues"]
    linked = pair == video_pair and position is not None and position.uses_music_clock
    at = position.local_seconds if linked else timeline_sample(song)
    phone = PhonePreview(card, routine, section_title(song), "音乐", 0)
    focus = phone.timeline(rows, at)
    phone.transport(at, song["duration"])
    cue = rows[focus] if focus is not None else rows[0]
    card.text(511, 143, section_title(song), 24, ACCENT)
    card.text(511, 181, song.get("title") or song["section"], 40, limit=620)
    card.text(511, 253, f"小节内 {clock(at)}  /  {clock(song['duration'])}", 28)
    if linked:
        card.text(511, 295, "与视频定位预览为同一时点", 23, SECONDARY)
    card.draw.rounded_rectangle((511, 353, 1135, 663), 15, fill=PANEL)
    caption = "当前动作" if focus is not None else "首个动作"
    card.text(535, 374, f"{caption}   {clock(cue['time'])}", 26, ACCENT)
    y = 432
    for line in wrapped_lines(card.draw, cue["action"], card.font(26), 573, 5):
        card.text(535, y, line, 26, limit=573)
        y += 33
    details = [f"原谱第 {cue['page']} 页"]
    if counts := score_metadata(cue.get("counts")):
        details.append(f"{counts} 拍")
    if repetitions := score_metadata(cue.get("repetitions")):
        details.append(f"REPS {repetitions}")
    card.text(535, 620, "  ·  ".join(details), 23, SECONDARY, 573)
    card.text(511, 693, f"{sum(row['cueRows'] > 0 for row in tracks)} 首 · 共 {sum(row['cueRows'] for row in tracks)} 条动作",
              24, SECONDARY)
    return at, cue["page"]


def original_pdf_card(card, routine, timeline, cue_position, files, pages, with_pages, report):
    at, cue_page = cue_position or (0, None)
    preferred = {**timeline[1], "pdfPage": cue_page} if timeline and cue_page else None
    samples = []
    for row in (preferred, *[with_pages[index] for index in (0, len(with_pages) // 2, -1) if with_pages]):
        if row and all(row["pdfPage"] != item["pdfPage"] for item in samples):
            samples.append(row)
    lead = samples[0] if samples else None
    page = lead["pdfPage"] if lead else 1
    phone = PhonePreview(card, routine, section_title(lead) if lead else None, "音乐", 1)
    if not pages:
        detail = "原谱需复查" if "pdf" in files else "包中没有原谱"
        phone.empty(detail)
        card.text(511, 160, detail, 40)
        return
    try:
        phone.pdf(opening_preview(files["pdf"], page), page, pages)
        if lead:
            phone.transport(at, lead["duration"])
        card.text(511, 143, section_title(lead) if lead else "原谱", 24, ACCENT)
        card.text(511, 181, f"第 {page} 页  /  共 {pages} 页", 40)
        card.text(511, 251, f"包内已标注 {len(with_pages)} 个小节入口", 24, SECONDARY)
        for index, row in enumerate(samples[1:]):
            x = 511 + index * 210
            card.media(opening_preview(files["pdf"], row["pdfPage"]), (x, 326, 190, 272))
            card.text(x, 626, f"{section_title(row)} · 第 {row['pdfPage']} 页", 22, limit=200)
    except (ValueError, OSError, pypdfium2.PdfiumError):
        add_issue(report, "pdf", "原谱抽查页无法渲染。")
        phone.empty("原谱需修正")
        card.text(511, 160, "原谱需修正", 40, WARNING)


def screenshot_cards(manifest, files, report, font_path):
    tracks = report["tracks"]
    art = f"{manifest['program']} {manifest['routine']}"
    chapter_set = [entry for entry in tracks if entry["chapter"]]
    measured = sum(entry["status"] == "measured" for entry in tracks)
    with_pages = [entry for entry in tracks if entry["pdfPage"]]
    with_cues = sum(entry["cueRows"] for entry in tracks)
    marker = report["sha256"][:12]
    def decorate(panel, y, *keys, reason_x=48, show_note=True):
        state, outcome = delivery_display(manifest, report, *keys)
        panel.status(state, y)
        notes = outcome["blockers"] or outcome["limitations"]
        if notes and show_note:
            prefix = f"{len(notes)} 项 · " if len(notes) > 1 else ""
            color = SECONDARY if state == "passed" else WARNING
            panel.text(reason_x, 724, prefix + notes[0]["detail"], 21, color, 1152 - reason_x)

    def card(number, title):
        return Card(title, f"{number:02d} / 09     {art}     SHA-256 {marker}", font_path)

    def preview_card(number, title):
        preview = card(number, None)
        preview.text(48, 30, title, 48)
        preview.draw.line((48, 96, 1152, 96), fill=PANEL, width=2)
        return preview

    counts = card(2, "包内资料")
    picture = report.get("videoInfo")
    pages = report.get("pdfPageCount")
    video_format = (f"{picture['width']}×{picture['height']} · {picture['codec'].upper()} · {picture['bitrateKbps']:,} kb/s"
                    if picture else "待核对" if "video" in files else "未附视频")
    caption = "未附视频，不适用"
    if picture:
        caption_parts = subtitle_summary(picture, report["screenSubtitles"])
        caption = " · ".join(caption_parts)
    templates = report.get("classTemplates", [])
    template_rows = [("排课模板", f"{len(templates)} 套 · " + " / ".join(t["name"] for t in templates))] if templates else []
    counts.rows([("音乐", f"{len(tracks)} 首"), ("视频", video_format), ("字幕", caption),
                 ("原谱", f"{pages} 页" if pages else "待核对" if "pdf" in files else "未附原谱"),
                 ("封面", "已包含" if "cover" in files else "App 会用占位图"),
                 ("视频定位", f"{len(chapter_set)} 个 · 实测吻合 {measured} 个"),
                 ("原谱入口", f"{len(with_pages)} 个物理页入口"),
                 ("小节功能", f"{sum(bool(row['sectionFunction']) for row in tracks)} / {len(tracks)} 首已有定义"),
                 *template_rows,
                 ("动作", f"{with_cues} 条"),
                 ("完整性", f"{report['bytes']:,} 字节 · 所有载荷 SHA-256 已核对")])
    decorate(counts, 85, "contents", "subtitles")
    yield "02-inventory.png", counts.image

    names = {"standard": "正曲", "alternative": "替换", "bonus": "加曲"}
    music = card(3, "曲目与小节功能")
    music_rows = []
    for row in tracks:
        label = f"{row['section']}  {names.get(row['variant'], row['variant'])}"
        detail = f"{clock(row['duration'])}   {row['title'] or row['section']}"
        if function := row["sectionFunction"]:
            checked = row.get("sectionFunctionCheck")
            status = " · 需人工复核" if checked == "manual" else " · 与原谱矛盾" if checked == "contradiction" else ""
            detail = f"{function['name']} · 第 {function['page']} 页{status} · {detail}"
        music_rows.append((label, detail))
    music.rows(music_rows)
    decorate(music, 85, "audio", "structure")
    yield "03-track-list.png", music.image

    video_pair, timeline_pair = select_preview_tracks(manifest["music"], tracks)
    video = preview_card(4, "视频定位")
    position = video_seek_card(video, art, video_pair, files, report)
    decorate(video, 33, "video", reason_x=511)
    yield "04-video-seek.png", video.image

    timing = card(5, "音乐与视频时钟复测")
    if picture and measured:
        timing.text(48, 695, "前／中／尾三个窗口 · 数值为相对包内定位偏移（ms）", 23, SECONDARY)
    timing_rows = []
    for row in tracks if picture else ():
        values = " / ".join(f"{window['offsetMS']:+.1f}" for window in row["windows"])
        result = "收尾曲 · 仅音乐" if row.get("supplementary") and not row["chapter"] else TIMING_STATES[row["status"]]
        timing_rows.append((section_title(row), values + " ms" if values else result))
    timing.rows(timing_rows or [("时钟复测", "本包未提供可复测的视频。")])
    decorate(timing, 85, "video", show_note=bool(delivery_display(manifest, report, "video")[1]["blockers"]))
    yield "05-timing.png", timing.image

    cues = preview_card(6, "动作时间线")
    cue_position = action_timeline_card(cues, art, timeline_pair, video_pair, position, tracks)
    decorate(cues, 33, "timeline", reason_x=511)
    yield "06-action-timeline.png", cues.image

    pdf = preview_card(7, "原版波形图")
    original_pdf_card(pdf, art, timeline_pair, cue_position, files, pages, with_pages, report)
    decorate(pdf, 33, "pdf", reason_x=511)
    yield "07-source-pdf.png", pdf.image

    volume = card(8, "音乐响度与峰值")
    level_rows = []
    for row in tracks:
        detail = (f"{row['lufs']:.1f} LUFS    {row['truePeakDBTP']:.1f} dBTP"
                  if row["decoded"] else "解码或测量需复核")
        if row.get("loudnessReview"):
            detail += " · 待听"
        level_rows.append((section_title(row), detail))
    if report.get("loudnessPolicy") == "keepOriginalDynamics":
        volume.text(43, 172, "保留音乐相对响度 · 核对解码与真峰值", 20, SECONDARY)
    volume.rows(level_rows, top=204 if report.get("loudnessPolicy") == "keepOriginalDynamics" else 180)
    decorate(volume, 85, "audio")
    yield "08-loudness.png", volume.image

    # Video frames and PDF previews can add issues during rendering.
    overview = Card(None, f"01 / 09     {art}     SHA-256 {marker}", font_path)
    routine_overview(overview, art, manifest, files, report)
    yield "01-identity.png", overview.image

    gaps = card(9, "课型识别")
    format_state, _ = delivery_display(manifest, report, "formats")
    if templates or format_state != "passed":
        gaps.status(format_state, 85)
    if templates:
        per_column = 7 if len(templates) <= 7 else 6
        width = 1070 if len(templates) <= 7 else 520
        row_height = 75 if len(templates) == 7 else 87
        checks = report["checks"]["formats"]
        for index, template in enumerate(templates):
            column, row = divmod(index, per_column)
            x, y = 48 + column * 552, 172 + row * row_height
            gaps.draw.rounded_rectangle((x, y, x + width, y + row_height - 6), 8, fill=PANEL)
            prefix = f"{template['name']} ·"
            notes = [note for note in checks["conditions"] if note.startswith(prefix)]
            failed = any(note.startswith(prefix) for note in checks["issues"])
            state = "failed" if failed or notes else "passed"
            gaps.text(x + 12, y + 5, f"{template['name']} · 原谱第 {template['page']} 页", 23, ACCENT, width - 82)
            gaps.status(state, y + 6, x=x + width - 64, marker_only=True)
            order = " → ".join(class_slot_label(slot, show_edition=True) for slot in template["slots"])
            gaps.text(x + 12, y + 31, order, 22, PRIMARY, width - 80)
            if notes or failed:
                note = notes[0].removeprefix(template["name"]).strip(" ·") if notes else "与原谱的选曲顺序不一致。"
                gaps.text(x + 12, y + (51 if row_height < 80 else 56), note, 18, WARNING, width - 22)
        pending = [note for note in checks["conditions"] if not any(
            note.startswith(f"{template['name']} ·") for template in templates)]
        if pending:
            marker = f"另有 {len(pending)} 项待复核 · " if len(pending) > 1 else ""
            gaps.text(48, 704, marker + pending[0], 21, WARNING, 1104)
        gaps.text(48, 733 if pending else 719, "在实际 App 验收导入和跨套路排课", 21, SECONDARY)
    else:
        checks = report["checks"]
        prioritized = ["formats", *(key for key in checks if key != "formats")]
        notes = [(CHECK_LABELS[key], reason) for key in prioritized
                 for reason in [*checks[key]["issues"], *checks[key]["conditions"]]]
        gaps.rows([("课型", "包内未提供已核对的原谱课型"), *notes,
                   *(("编号间隔", f"T{number} · 对照原谱确认") for number in report["sectionGaps"]),
                   ("App", "导入、播放与排课需在实际 App 中验收")])
    yield "09-review.png", gaps.image


def generate(args, *, sealed_copy=None, overview_only=False):
    target_lufs = getattr(args, "target_lufs", None)
    keep_music = bool(getattr(args, "keep_music", False))
    require(not keep_music or target_lufs is None, "Choose original dynamics or a loudness target.")
    require(target_lufs is None or -24 <= target_lufs <= -12,
            "Select a loudness reference between -24 and -12 LUFS")
    root = args.output_dir
    root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="ssroutine-qa-") as staging, \
         tempfile.TemporaryDirectory(dir=root, prefix=".ssroutine-report-") as report_staging:
        work = Path(staging)
        manifest, audio, optional, package_bytes, full_sha, manifest_sha = source_files(args.package, work, sealed_copy)
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
                  "bytes": package_bytes, "sha256": full_sha, "manifestSHA256": manifest_sha,
                  "sectionGaps": sorted(set(range(1, max(sections) + 1)) - sections),
                  "appImportVerified": False, "sourceLimited": bool(getattr(args, "source_limited", False)),
                  "tracks": inspect_tracks(manifest, audio, optional),
                  "blockingIssues": [], "checks": {
                      key: {"issues": [], "conditions": []} for key in CHECK_LABELS
                  }}
        output = Path(report_staging) / "evidence"
        output.mkdir()
        tracks = report["tracks"]
        grit = None
        if "pdf" in optional:
            try:
                if manifest["program"] == "GRIT":
                    grit = grit_source_evidence(optional["pdf"], manifest["music"], manifest["routine"])
            except (ValueError, OSError):
                add_issue(report, "pdf", "无法读取原谱，不能核实小节资料的覆盖范围。")
        supplemental = {section for section, record in grit["tracks"].items() if record["supplement"]} if grit else set()
        def is_supplement(track):
            return track["variant"] == "standard" and track["section"] in supplemental

        missing_chapters = [track for track in tracks if not track["chapter"] and not is_supplement(track)]
        uncertain_chapters = [track for track in tracks if track["status"] in {"approximate", "visuallyConfirmed"}]
        missing_cues = [track for track in tracks if not track["cueRows"] and not is_supplement(track)]
        missing_functions = [track for track in tracks if not track["sectionFunction"]]
        if missing_functions:
            detail = ("本包未提供可核实的小节功能。" if len(missing_functions) == len(tracks)
                      else f"{sections_for(missing_functions)} 未提供可核实的小节功能。")
            if "pdf" in optional:
                add_condition(report, "structure", detail)
            else:
                add_source_limitation(report, "structure", "no_source_functions", detail)
        if "video" not in optional:
            add_source_limitation(report, "video", "no_source_video", "本包未附视频。音乐可以播放和排课。")
        elif missing_chapters:
            add_condition(report, "video", f"{sections_for(missing_chapters)} 无可靠小节定位。可按整段视频观看。")
        if uncertain_chapters:
            add_condition(report, "video", f"{sections_for(uncertain_chapters)} 仅有近似或画面位置，音画时钟未对齐。")
        if missing_cues and "pdf" not in optional:
            detail = ("本包未附动作时间线。" if len(missing_cues) == len(tracks)
                      else f"{sections_for(missing_cues)} 未提供可用动作时间线。")
            add_source_limitation(report, "timeline", "no_timed_source", detail + "音乐仍可播放和排课。")
        for track in tracks:
            if is_supplement(track):
                track["supplementary"] = True
                label = section_title(track)
                if "video" in optional and not track["chapter"]:
                    add_source_limitation(report, "video", "supplementary_outro",
                                          f"{label} 为原谱列出的收尾曲，保留音乐及原谱入口。")
                if not track["cueRows"]:
                    add_source_limitation(report, "timeline", "supplementary_outro",
                                          f"{label} 为原谱列出的收尾曲，无教学动作表。")
        if "pdf" not in optional:
            add_source_limitation(report, "pdf", "no_source_pdf", "本包未附原版波形图或教学讲义。")
        elif missing_pages := [track for track in tracks if not track["pdfPage"]]:
            add_condition(report, "pdf", f"{sections_for(missing_pages)} 无指定页入口。可浏览完整原谱。")
        for song in tracks:
            slot = f"{song['section']} · {song['variant']}"
            if not song["decoded"]:
                add_issue(report, "audio", f"{slot} 音乐无法解码或无可测声音。")
            elif not song["durationMatches"]:
                add_issue(report, "audio", f"{slot} 实际音乐时长与清单不符。")
            if song["status"] == "review":
                add_issue(report, "video", f"{slot} 包内音画复测未通过。{timing_failure_detail(song)}。")
        inspect_loudness(report, target_lufs, keep_music=keep_music)
        if "video" in optional:
            try:
                picture = video_info(optional["video"])
                require(abs(picture["duration"] - manifest["video"]["duration"]) <= 0.25,
                        "Video duration does not match")
            except (ValueError, OSError):
                add_issue(report, "video", "视频流无法读取，或实际视频时长与清单不符。")
            else:
                report["videoInfo"] = picture
                screen = inspect_screen_subtitles(optional["video"], picture["duration"],
                    None if overview_only else output / "subtitle-samples.jpg", getattr(args, "screen_subtitles", None))
                report["screenSubtitles"] = screen
                if screen["result"] in {"candidate", "review"}:
                    explanation = ("抽查发现疑似画面字幕。请看采样帧确认字幕、课程标题或水印。"
                                   if screen["result"] == "candidate" else
                                   "抽样的画面字幕需看帧确认。OCR 不可用或未完成全部采样。")
                    add_condition(report, "subtitles", explanation)
        if "pdf" in optional:
            try:
                with pypdfium2.PdfDocument(optional["pdf"]) as document:
                    require(len(document) == manifest["pdf"]["pageCount"], "Original PDF page count does not match")
                    report["pdfPageCount"] = len(document)
                _, proposals, _ = extract_cues(optional["pdf"], manifest["music"])
                if missing_cues:
                    unmatched_cues = missing_cues
                    if report["sourceLimited"]:
                        unknown = []
                        for source, track in zip(proposals, tracks, strict=True):
                            if track not in missing_cues:
                                continue
                            if page := source.get("untimedScorePage"):
                                add_source_limitation(report, "timeline", "no_timed_score",
                                    f"{section_title(track)} 原谱第 {page} 页为教学说明，没有分段时点。")
                            else:
                                unknown.append(track)
                        if len(unknown) == len(tracks) and readable_untimed_score(optional["pdf"], tracks):
                            add_source_limitation(report, "timeline", "no_timed_score",
                                                  "本包未附动作时间线。可读原谱中没有分段时间。")
                            unknown = []
                        unmatched_cues = unknown
                    if unmatched_cues:
                        add_condition(report, "timeline",
                            f"{sections_for(unmatched_cues)} 未提供可用动作时间线。请核对原谱中的时间表或分段时长。")
                intervals = grit_interval_proposals(optional["pdf"], manifest["music"], grit)
                templates = []
                if source_file := optional.get("classTemplatesFile"):
                    with source_file.open("rb") as stream:
                        templates = read_class_template_document(stream, manifest["pdf"])
                    report["classTemplates"] = templates
                try:
                    for note in check_class_template_sources(optional["pdf"], templates, include_omissions=True,
                                                             source_evidence=grit):
                        add_condition(report, "formats", note)
                except ValueError as error:
                    add_issue(report, "formats", str(error))
                for template in templates:
                    for note in class_recording_conditions(template, manifest["music"]):
                        add_condition(report, "formats", note)
                for song, source, track in zip(manifest["music"], proposals, tracks):
                    grit_record = grit["tracks"].get(song["section"]) if grit and song["variant"] == "standard" else None
                    if grit_record and track["pdfPage"] is not None and track["pdfPage"] not in grit_record["openingPages"]:
                        add_issue(report, "pdf", f"{song['section']} 的原谱入口与对应课型和录音不符。")
                    verified_role = check_grit_section_function(song, grit) if grit_record else None
                    if checked := verified_role or source.get("sectionFunctionCheck"):
                        status, message = checked
                        track["sectionFunctionCheck"] = status
                        if status == "contradiction":
                            add_issue(report, "structure", message)
                        if status == "manual":
                            add_condition(report, "structure", message)
                    has_printed_timeline = source["rows"] and source["status"] != "unresolved"
                    if interval := intervals.get(song["section"]):
                        state, detail = validate_interval_review(interval, song.get("cues") or [])
                        if state == "contradiction":
                            add_issue(report, "timeline", f"{song['section']} · {detail}")
                        elif state != "matched":
                            add_condition(report, "timeline", f"{song['section']} · {detail}")
                    elif has_printed_timeline and not song.get("cues"):
                        add_condition(report, "timeline", f"{song['section']} 原谱有可读时间表，包内未提供对应的动作时间线。")
                    elif has_printed_timeline:
                        missing = missing_printed_cue_times(source["rows"], song["cues"])
                        if missing:
                            at = "、".join(clock(time) for time in missing[:4])
                            add_condition(report, "timeline",
                                          f"{song['section']} 缺 {len(missing)} 个原谱动作起点（{at}）。核对时间和动作后补齐。")
            except (ValueError, OSError, pypdfium2.PdfiumError):
                add_issue(report, "pdf", "原谱无法打开，或实际页数与清单不符。")
        for key, reason in manifest.get("qualityNotes", {}).items():
            if report["checks"][key]["conditions"]:
                add_condition(report, key, "制作者说明 · " + reason)
        width, height = SIZE
        gap = 16
        contact = Image.new("RGB", (3 * width + 4 * gap, 3 * height + 4 * gap), BACKGROUND)
        for file, frame in screenshot_cards(manifest, optional, report, font_path):
            index = int(file[:2]) - 1
            if not overview_only:
                frame.save(output / file)
            contact.paste(frame, (gap + (index % 3) * (width + gap), gap + (index // 3) * (height + gap)))
        contact.save(output / "3x3-overview.png")
        report["qaStatus"] = check_state(report)
        report["offlineMediaPassed"] = report["qaStatus"] != "failed"
        report["delivery"] = assess_delivery(manifest, report, source_limited=report["sourceLimited"])
        if not overview_only:
            write_html(output, manifest, report)
        (output / "qa.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        output.rename(destination)
    print(json.dumps({"output": str(destination.resolve()), "screenshots": 9, "sha256": full_sha,
                      "measuredChapters": sum(row["status"] == "measured" for row in report["tracks"]),
                      "offlineMediaPassed": report["offlineMediaPassed"], "status": report["qaStatus"],
                      "deliveryStatus": report["delivery"]["status"],
                      "blockingIssues": len(report["blockingIssues"])}))
    return report["offlineMediaPassed"] and (not getattr(args, "strict", False)
                                             or report["delivery"]["status"] != "blocked")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("package", type=Path)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--credit", help="Display credit for this evidence set")
    parser.add_argument("--made-at", help="Verified creation time in ISO 8601; omit to use evidence generation time")
    parser.add_argument("--font", type=Path, help="Optional font with Chinese glyphs")
    loudness = parser.add_mutually_exclusive_group()
    loudness.add_argument("--target-lufs", type=float, metavar="LUFS",
                          help="The producer's chosen loudness target; omit to compare tracks within the package")
    loudness.add_argument("--keep-music", action="store_true",
                          help="Preserve relative loudness; still check decoding and true peaks")
    parser.add_argument("--source-limited", action="store_true",
                        help="Assess a creator-confirmed scope with genuinely absent original materials")
    parser.add_argument("--screen-subtitles", choices=("present", "absent"), metavar="RESULT",
                        help="Visual judgment after reviewing samples from this exact package")
    parser.add_argument("--strict", action="store_true", help="Write full evidence and exit 2 if delivery is blocked")
    args = parser.parse_args()
    try:
        passed = generate(args)
        if args.strict and not passed:
            parser.exit(2, "Package delivery is blocked. See report.html.\n")
    except (ValueError, OSError, TypeError, KeyError) as error:
        parser.exit(1, f"{error}\n")


if __name__ == "__main__":
    main()
