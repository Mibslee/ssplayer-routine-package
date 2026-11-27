"""Read course-format columns from the printed page coordinates."""

from contextlib import closing
from dataclasses import dataclass, replace
from io import BytesIO
from pathlib import Path
import re
from statistics import median
import subprocess
import unicodedata


ENGLISH_HEADER = re.compile(
    r"^(\d{1,3})\s*[-‐‑– ]?\s*MIN(?:UTE)?S?\s+"
    r"(EXPRESS\s+FORMAT|EXPRESS(?=\s*$)|FORMAT|STRENGTH|FLEXIBILITY)\b"
    r"(?:\s*[-‐‑–—]\s*OPTION\s+(\d{1,2}))?", re.I)
SPANISH_HEADER = re.compile(r"^(FUERZA|FLEXIBILIDAD)\s+(\d{1,3})\s*MINUTOS?\b", re.I)
TOTAL_LINE = re.compile(r"^(?:TOTAL\s+TIME|TIEMPO\s+(?:TOTAL|\d{1,3}:)|合计|总时长)", re.I)
LEGEND = re.compile(r"^(?:KEY|CLAVE|LÉGENDE)\b", re.I)
SIDEBAR = re.compile(r"^(?:PLEASE NOTE|NOTA IMPORTANTE)\b|^请注意", re.I)


def format_header(line):
    if match := ENGLISH_HEADER.match(line):
        minutes = int(match[1])
        return (minutes, match[2].upper(), match[3]) if 5 <= minutes <= 180 else None
    if match := SPANISH_HEADER.match(line):
        minutes = int(match[2])
        kind = "STRENGTH" if match[1].upper() == "FUERZA" else "FLEXIBILITY"
        return (minutes, kind, None) if 5 <= minutes <= 180 else None
    return None


def line_text(row):
    return unicodedata.normalize("NFKC", " ".join(word["text"] for word in row)).strip()


@dataclass(frozen=True)
class FormatLine:
    text: str
    left: float = 0
    top: float = 0
    height: float = 0
    ocr_words: tuple = ()

    @classmethod
    def positioned(cls, row):
        top = min(word["top"] for word in row)
        bottom = min(word["bottom"] for word in row)
        return cls(line_text(row), row[0]["x0"], top, bottom - top,
                   tuple(row) if row[0].get("ocr") else ())


def join_wrapped_track_labels(column):
    """Reconnect a track number wrapped within the same printed number column."""
    pending = None
    for line in column:
        if pending:
            width = max(14, pending.height * 1.85)
            continuation = re.fullmatch(r"\d{1,3}[A-Z]?(?:\s*/\s*\d{1,3}[A-Z]?)*", line.text, re.I)
            prefix = re.match(r"^(TRACK\s*(?:\d{1,3}[A-Z]?\s*/\s*TRACK\s*)?)(.*)$", pending.text, re.I)
            if (prefix and continuation and abs(pending.left - line.left) <= 4
                    and 0 < line.top - pending.top <= width):
                yield replace(pending, text=f"{prefix[1].strip()} {line.text} {prefix[2]}".strip())
                pending = None
                continue
            yield pending
        pending = line
    if pending:
        yield pending


def format_columns(words, width, row_lines, source_row):
    """Use repeated table starts and track starts to identify reading lanes.

    A page midpoint may be inside a course table, with a legend occupying the
    other half. Anchor on actual format or track labels and stop at the legend.
    """
    gap = max(4, min(8, width * 0.01))
    markers = []
    sidebars = []
    for row in row_lines(words):
        for index, word in enumerate(row):
            if index and word["x0"] - row[index - 1]["x1"] < gap:
                continue
            line = line_text(row[index:])
            if format_header(line):
                markers.append((word["x0"], word["top"], True))
            elif source_row.match(line) or TOTAL_LINE.match(line):
                markers.append((word["x0"], word["top"], False))
            elif (is_legend := bool(LEGEND.match(line))) or SIDEBAR.match(line):
                sidebars.append((word["x0"], word["top"], is_legend))
    headings = [(x, top) for x, top, is_header in markers if is_header]
    if not headings:
        return []
    first_heading = min(top for _, top in headings)
    rightmost_heading = max(x for x, _ in headings)
    legend = min((x for x, top, is_legend in sidebars
                  if is_legend and top < first_heading and x - rightmost_heading >= width * 0.12), default=width)
    groups = []
    for x, _, is_header in sorted(markers):
        if x >= legend:
            continue
        if not groups or x - groups[-1][0][0] > 2 * gap:
            groups.append([])
        groups[-1].append((x, is_header))
    # A lane without a heading is valid evidence of column flow only if
    # several track or duration rows agree on where it starts.
    anchors = [group[0][0] for group in groups
               if any(is_header for _, is_header in group) or len(group) >= 3]
    # Adjacent annotations and table borders can sit just to the left of a
    # row label. Leave that margin with the column it belongs to.
    first_lane = anchors[0] - gap if anchors[0] >= width * .40 else 0
    left_edges = [first_lane, *(x - gap for x in anchors[1:])]
    limit = min((x - gap / 2 for x, _, _ in sidebars if x - anchors[-1] >= width * 0.12), default=width)
    right_edges = [*left_edges[1:], limit]
    return [list(map(FormatLine.positioned, row_lines([word for word in words if start <= word["x0"] < end])))
            for start, end in zip(left_edges, right_edges, strict=True)]


def recheck_ocr_track_labels(columns, source, number, source_row):
    """Read only unclear OCR track labels in the printed number column."""
    work = []
    for column in columns:
        numbered = [line.ocr_words[2]["x0"] for line in column
                    if len(line.ocr_words) >= 3
                    and line.ocr_words[0]["text"].casefold() == "track"
                    and re.fullmatch(r"\d{1,3}[A-Z]?(?:[&+]\d{0,3}[A-Z])*", line.ocr_words[1]["text"], re.I)]
        if len(numbered) < 2:
            continue
        title_left = median(numbered)
        if max(numbered) - min(numbered) > 8:
            continue
        for index, line in enumerate(column):
            if (len(line.ocr_words) < 2 or line.ocr_words[0]["text"].casefold() != "track"
                    or source_row.match(line.text)):
                continue
            track = line.ocr_words[0]
            if not 12 < title_left - track["x1"] < 85:
                continue
            title = " ".join(word["text"] for word in line.ocr_words[1:] if word["x0"] > title_left - 5)
            raw_label = "".join(word["text"] for word in line.ocr_words[1:] if word["x0"] <= title_left - 5)
            parts = re.search(r"([A-Z](?:[&+]\d{0,3}[A-Z])+)[^\w]*_?$", raw_label.upper())
            printed_parts = re.findall(r"[A-Z]", parts[1]) if parts else []
            if title:
                work.append((column, index, track, title_left, title, printed_parts))
    if not work:
        return

    import pypdfium2
    from PIL import ImageOps

    if hasattr(source, "read"):
        offset = source.tell()
        try:
            source.seek(0)
            data = source.read()
        finally:
            source.seek(offset)
    else:
        data = Path(source).read_bytes()
    scale = 5
    with pypdfium2.PdfDocument(data) as document, closing(document[number - 1]) as page:
        with closing(page.render(scale=scale)) as bitmap:
            image = bitmap.to_pil()
            for column, index, track, title_left, title, printed_parts in work:
                bounds = (track["x1"] + 1, track["top"] - 2, title_left - 2, track["bottom"] + 3)
                crop = image.crop(tuple(round(value * scale) for value in bounds))
                picture = BytesIO()
                ImageOps.expand(crop, border=12, fill="white").save(picture, format="PNG")
                letters = "".join(dict.fromkeys(printed_parts)) or "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
                try:
                    decoded = subprocess.run(("tesseract", "stdin", "stdout", "--psm", "7", "-c",
                                              "tessedit_char_whitelist=0123456789" + letters + "&+/"),
                                             input=picture.getvalue(), capture_output=True, check=True, timeout=8)
                except (OSError, subprocess.SubprocessError):
                    continue
                label = "".join(decoded.stdout.decode(errors="replace").upper().split())
                if printed_parts and re.findall(r"[A-Z]", label) != printed_parts:
                    continue
                observed = "Track " + label
                if match := source_row.fullmatch(observed):
                    column[index] = replace(column[index], text=match[0] + " " + title)


def matches_printed_role(label, title):
    def tokens(value):
        normalized = unicodedata.normalize("NFKD", value).casefold()
        return re.findall(r"\w+\.?", "".join(c for c in normalized if not unicodedata.combining(c)))

    actual, printed = tokens(label), tokens(title)
    return bool(actual) and len(actual) == len(printed) and all(
        word.rstrip(".") == source.rstrip(".")
        or (source.endswith(".") and len(source) >= 4 and word.startswith(source[:-1]))
        for word, source in zip(actual, printed))


def equivalent_format(template):
    """Compare source rules across language editions while keeping true options."""
    if template.get("sourceReview"):
        return None
    return (template["minutes"], template["_kind"], template["_option"], tuple(
        (tuple(slot["sectionNumbers"]), tuple(slot.get("sectionCodes", ())),
         slot.get("selectionMode", "single"), slot.get("isOptional", False),
         slot.get("variantPreference"), slot.get("enforcesVariant", False),
         slot.get("requiredDurationMinutes")) for slot in template["slots"]))


def combine_equivalent_pages(templates):
    result = []
    seen = {}
    for template in templates:
        signature = equivalent_format(template)
        previous = seen.get(signature)
        if (signature is not None and previous is not None
                and template["page"] != previous["page"]):
            pages = previous.setdefault("sourcePages", [previous["page"]])
            if template["page"] not in pages:
                pages.append(template["page"])
            continue
        result.append(template)
        if signature is not None:
            seen[signature] = template
    return result
