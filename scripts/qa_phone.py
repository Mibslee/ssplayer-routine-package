"""Render an offline preview using the rehearsal screen's reading order."""

from dataclasses import dataclass
from math import isfinite

from PIL import Image, ImageDraw, ImageOps


INK = "#131821"
MUTED = "#616876"
SEPARATOR = "#e2e5eb"
PHONE = "#eff1f6"
RED = "#e9254b"
HIGHLIGHT = "#fbebee"
FONT_PATHS = (
    "/System/Library/Fonts/STHeiti Medium.ttc",
    "/System/Library/Fonts/STHeiti Light.ttc",
    "C:/Windows/Fonts/msyh.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
)
VARIANTS = {"alternative": "替换曲", "bonus": "加曲", "combo45": "45 分钟版",
            "combo45Alternative": "45 分钟替换曲", "combo45Bonus": "45 分钟加曲",
            "outro": "结束曲", "preClass": "课前曲"}


def clock(seconds):
    minutes, whole = divmod(int(max(0, seconds)), 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours}:{minutes:02d}:{whole:02d}" if hours else f"{minutes}:{whole:02d}"


def section_title(song):
    variant = VARIANTS.get(song["variant"])
    return f"{song['section']} · {variant}" if variant else song["section"]


def active_row(rows, seconds):
    return next((index for index in range(len(rows) - 1, -1, -1) if rows[index]["time"] <= seconds), None)


def repeat_title(row):
    title = f"Block {row['block']}"
    return f"{title} · 重复 Block {row['repeatsBlock']}" if row.get("repeatsBlock") is not None else title


def score_metadata(value):
    return " / ".join(line.strip() for line in (value or "").splitlines() if line.strip())


def timeline_sample(song):
    rows = song.get("cues") or []
    row = next((row for row in rows if row.get("repeatsBlock") is not None), rows[0] if rows else None)
    return row["time"] if row is not None else 0.0


@dataclass(frozen=True)
class VideoPosition:
    source_seconds: float
    local_seconds: float
    local_duration: float
    uses_music_clock: bool


def video_position(song, track, video_duration):
    chapter = track["chapter"] if track else None
    if not chapter:
        point = min(3.0, video_duration / 2)
        return VideoPosition(point, point, video_duration, False)
    interval = chapter["end"] - chapter["start"]
    if track["status"] != "measured" or not track["windows"]:
        elapsed = min(interval / 2, 12.0)
        return VideoPosition(chapter["start"] + elapsed, elapsed, interval, False)
    duration = song["duration"]
    rows = song.get("cues") or []
    repeated = next((index for index, row in enumerate(rows) if row.get("repeatsBlock") is not None), None)
    preferred = duration / 2
    samples = [point["position"] * duration for point in track["windows"]]
    if repeated is not None:
        start = rows[repeated]["time"]
        end = next((row["time"] for row in rows[repeated + 1:] if row.get("block") is not None), duration)
        within_repeat = [point for point in samples if start <= point < end]
        if within_repeat:
            samples = within_repeat
            preferred = start
    elapsed = min(samples, key=lambda point: abs(point - preferred))
    return VideoPosition(chapter["start"] + elapsed, elapsed, duration, True)


def select_preview_tracks(songs, tracks):
    indexed = list(zip(songs, tracks))

    def cue_detail(song):
        rows = song.get("cues") or []
        return (sum(row.get("repeatsBlock") is not None for row in rows),
                sum(bool(row.get("level")) for row in rows), len(rows))

    video = max((pair for pair in indexed if pair[1]["chapter"]),
                key=lambda pair: (pair[1]["status"] == "review", pair[1]["status"] == "measured",
                                  cue_detail(pair[0])), default=None)
    timeline = (video if video and video[0].get("cues") else
                max((pair for pair in indexed if pair[0].get("cues")),
                    key=lambda pair: cue_detail(pair[0]), default=None))
    return video, timeline


def wrapped_lines(draw, value, font, width, limit):
    """Keep written action breaks and wrap both spaced and unspaced text."""
    lines = []
    for paragraph in str(value).splitlines():
        remaining = " ".join(paragraph.split())
        while remaining:
            if len(lines) == limit:
                last = lines[-1]
                while last and draw.textlength(last + "…", font=font) > width:
                    last = last[:-1].rstrip()
                return [*lines[:-1], last + "…"]
            end = 1
            while end < len(remaining) and draw.textlength(remaining[:end + 1], font=font) <= width:
                end += 1
            if end < len(remaining) and (space := remaining.rfind(" ", 0, end + 1)) > 0:
                end = space
            lines.append(remaining[:end].strip())
            remaining = remaining[end:].lstrip()
    return lines


class PhonePreview:
    x, y, width, height = 64, 110, 382, 620
    padding = 12

    def __init__(self, card, routine, section, mode, selected_tab, frame=None, unavailable_video=None):
        self.card = card
        self.draw = card.draw
        self.left = self.x + self.padding
        self.right = self.x + self.width - self.padding
        self.bottom = self.y + self.height - 56
        self.draw.rounded_rectangle((self.x, self.y, self.x + self.width, self.y + self.height),
                                    25, fill=PHONE, outline="#a4abb8", width=2)
        self.header(routine, section, mode)
        tabs_y = self.y + 66
        if frame or unavailable_video:
            media_y, media_height = self.y + 62, 199 if frame else 94
            self.media(frame, (self.left, media_y, self.right - self.left, media_height),
                       background="#17202d" if frame else "#e0e4eb")
            if unavailable_video:
                self.text(self.left + 15, media_y + 36, unavailable_video, 19, MUTED)
            tabs_y = media_y + media_height + 12
        for index, title in enumerate(("动作时间线", "波形图", "笔记")):
            tab_width = (self.right - self.left) // 3
            tab_x = self.left + index * tab_width
            self.text(tab_x + tab_width // 2, tabs_y + 12, title, 19,
                      INK if index == selected_tab else MUTED, anchor="mt")
            if index == selected_tab:
                self.draw.rounded_rectangle((tab_x + 4, tabs_y + 38, tab_x + tab_width - 4, tabs_y + 41),
                                            2, fill=RED)
        self.content_y = tabs_y + 51

    def text(self, x, y, value, size, color=INK, width=None, anchor="lt"):
        font = self.card.font(size)
        if width is not None:
            lines = wrapped_lines(self.draw, value, font, width, 1)
            value = lines[0] if lines else ""
        self.draw.text((x, y), value, font=font, fill=color, anchor=anchor)

    def header(self, routine, section, mode):
        top = self.y + 13
        self.draw.rounded_rectangle((self.left, top, self.left + 103, top + 37), 18, fill="white", outline=SEPARATOR)
        self.draw.line([(self.left + 22, top + 11), (self.left + 15, top + 18), (self.left + 22, top + 25)],
                       fill=INK, width=3)
        for x, direction in ((self.left + 52, -1), (self.left + 85, 1)):
            self.draw.polygon([(x - 5 * direction, top + 12), (x + 5 * direction, top + 18),
                               (x - 5 * direction, top + 24)], fill=INK)
            self.draw.line((x + 7 * direction, top + 11, x + 7 * direction, top + 25), fill=INK, width=2)
        group_x = self.left + 117
        self.draw.rounded_rectangle((group_x, top, self.right, top + 37), 18, fill="white", outline=SEPARATOR)
        self.text(group_x + 12, top + 10, mode, 18, RED, 49)
        self.draw.line([(group_x + 53, top + 16), (group_x + 58, top + 21), (group_x + 63, top + 16)],
                       fill=RED, width=2)
        self.text(group_x + 73, top + 11, routine + (" · " + section if section else ""),
                  15, RED, self.right - group_x - 86)

    def media(self, image, box, background):
        x, y, width, height = box
        surface = Image.new("RGB", (width, height), background)
        if image:
            fitted = ImageOps.contain(image.convert("RGB"), (width, height), Image.Resampling.LANCZOS)
            surface.paste(fitted, ((width - fitted.width) // 2, (height - fitted.height) // 2))
        mask = Image.new("L", (width, height))
        ImageDraw.Draw(mask).rounded_rectangle((0, 0, width - 1, height - 1), 14, fill=255)
        self.card.image.paste(surface, (x, y), mask)

    def timeline(self, rows, at, linked=True):
        focus = active_row(rows, at) if linked else None
        if not rows:
            self.empty("缺少可用动作时间线")
            return focus

        def row_text(row):
            width = self.right - self.left - 34
            mapped = {value["actionLine"]: value["counts"] for value in row.get("actionRows", [])}
            action = []
            if mapped:
                for index, line in enumerate(row["action"].splitlines()):
                    count = mapped.get(index)
                    wrapped = wrapped_lines(self.draw, line, self.card.font(18), width - (56 if count else 0), 4)
                    action.extend((value, count if i == 0 else None) for i, value in enumerate(wrapped))
                action = action[:max(1, min(12, (self.bottom - self.content_y - 110) // 21))]
            else:
                action = [(value, None) for value in wrapped_lines(self.draw, row["action"], self.card.font(18), width, 4)]
            note = wrapped_lines(self.draw, row.get("coaching") or "", self.card.font(14), width, 2)
            return action, note, 35 + len(action) * 21 + len(note) * 16

        first = 0
        if focus is not None:
            first = max(0, focus - 2)
            available = self.bottom - self.content_y - 8
            while first < focus:
                visible = rows[first:focus + 1]
                header_space = 26 * (sum(row.get("block") is not None for row in visible)
                                     + (visible[0].get("block") is None))
                if sum(row_text(row)[2] for row in visible) + header_space <= available:
                    break
                first += 1
        y = self.content_y
        self.draw.rounded_rectangle((self.left, y, self.right, self.bottom - 6), 14, fill="white")
        block = next((index for index in range(first, -1, -1) if rows[index].get("block") is not None), None)
        if block is not None and block < first:
            y = self.block_header(rows[block], y)
        for index in range(first, len(rows)):
            row = rows[index]
            current = index == focus
            action, note, height = row_text(row)
            has_header = row.get("block") is not None
            if y + height + (26 if has_header else 0) > self.bottom - 8:
                break
            if has_header:
                y = self.block_header(row, y)
            if current:
                self.draw.rounded_rectangle((self.left + 2, y, self.right - 2, y + height),
                                            10, fill=HIGHLIGHT)
                self.draw.polygon([(self.left + 5, y + 14), (self.left + 5, y + 22), (self.left + 11, y + 18)],
                                  fill=RED)
            if level := row.get("level"):
                rgb = tuple(round(255 * level[key]) for key in ("red", "green", "blue"))
                self.draw.rounded_rectangle((self.right - 8, y + 5, self.right - 4, y + height - 5), 2, fill=rgb)
            self.text(self.left + 13, y + 10, clock(row["time"]), 17, RED if current else MUTED)
            counts = "" if row.get("actionRows") else score_metadata(row.get("counts"))
            repetitions = score_metadata(row.get("repetitions"))
            if repetitions:
                counts = f"{counts}  REPS {repetitions}".lstrip()
            if counts:
                self.text(self.right - 15, y + 11, counts, 14, MUTED, 126, "rt")
            if row.get("music"):
                self.text(self.left + 65, y + 11, row["music"], 14, RED if current else MUTED,
                          144 if counts else 268)
            detail_y = y + 32
            for value, count in action:
                self.text(self.left + 13, detail_y, value, 18)
                if count:
                    self.text(self.right - 15, detail_y, count, 14, MUTED, anchor="rt")
                detail_y += 21
            for value in note:
                self.text(self.left + 13, detail_y, value, 14, MUTED)
                detail_y += 16
            if not current:
                self.draw.line((self.left + 13, y + height, self.right - 13, y + height), fill=SEPARATOR)
            y += height
        return focus

    def block_header(self, row, y):
        self.text(self.left + 13, y + 7, repeat_title(row), 15, MUTED, self.right - self.left - 26)
        self.draw.line((self.left + 13, y + 24, self.right - 13, y + 24), fill=SEPARATOR)
        return y + 26

    def pdf(self, image, page, total):
        y = self.content_y
        self.text(self.left + 3, y + 4, "原版波形图", 19)
        self.text(self.right - 3, y + 5, f"{page} / {total}", 16, MUTED, anchor="rt")
        self.media(image, (self.left, y + 28, self.right - self.left, self.bottom - y - 37), background="white")

    def empty(self, message, detail=None):
        y = self.content_y
        self.draw.rounded_rectangle((self.left, y, self.right, self.bottom - 6), 14, fill="white")
        self.text(self.left + 15, y + 22, message, 19, INK, self.right - self.left - 30)
        if detail:
            self.text(self.left + 15, y + 56, detail, 15, MUTED, self.right - self.left - 30)

    def transport(self, at, duration):
        if not isfinite(duration) or duration <= 0:
            return
        y = self.bottom
        self.draw.rectangle((self.x + 3, y, self.x + self.width - 3, y + 32), fill="#f7f8fa")
        center = (self.left + 18, y + 24)
        self.draw.ellipse((center[0] - 17, center[1] - 17, center[0] + 17, center[1] + 17), fill=RED)
        self.draw.polygon([(center[0] - 4, center[1] - 7), (center[0] - 4, center[1] + 7),
                           (center[0] + 7, center[1])], fill="white")
        start, end = self.left + 49, self.left + 194
        dot = round(start + (end - start) * min(1, max(0, at / duration)))
        self.draw.rounded_rectangle((start, y + 22, end, y + 26), 2, fill="#c9ced6")
        if dot > start:
            self.draw.line((start, y + 24, dot, y + 24), fill=RED, width=4)
        self.draw.ellipse((dot - 7, y + 17, dot + 7, y + 31), fill="white", outline="#d8dce3")
        self.text(self.left + 209, y + 17, f"{clock(at)} / {clock(duration)}", 15, MUTED, 111)
        right = self.right - 5
        self.draw.line([(right - 15, y + 14), (right - 15, y + 33), (right - 9, y + 28),
                        (right - 3, y + 33), (right - 3, y + 14), (right - 15, y + 14)],
                       fill=INK, width=2)
