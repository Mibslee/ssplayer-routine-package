"""Positioned observations of original score pages, shared across authoring and QA."""

from collections import OrderedDict
from contextlib import closing
import csv
from hashlib import sha256
from io import BytesIO, StringIO
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import unicodedata


HEADER_ALIASES = {
    "MUSIC": "MUSIC", "MUSICA": "MUSIC", "MUSIQUE": "MUSIC", "MUSIK": "MUSIC",
    "EXERCISE": "EXERCISE", "EXERCISES": "EXERCISE", "EJERCICIO": "EXERCISE",
    "EJERCICIOS": "EXERCISE", "EXERCICE": "EXERCISE", "UBUNG": "EXERCISE",
    "SEQUENCE": "SEQUENCE", "SECUENCIA": "SEQUENCE", "SEQUENZ": "SEQUENCE",
    "CTS": "CTS", "CNTS": "CTS", "COUNTS": "CTS", "TEMPS": "CTS", "TIEMPOS": "CTS",
    "REPS": "REPS", "REPETICIONES": "REPS", "REPETITIONS": "REPS", "WIEDERHOLUNGEN": "REPS",
    "LEVEL": "LEVEL", "NIVEL": "LEVEL", "NIVEAU": "LEVEL", "STUFE": "LEVEL",
}


def normalized_label(value):
    value = unicodedata.normalize("NFKD", value).upper()
    return "".join(char for char in value if not unicodedata.combining(char))


def header_labels(value):
    return tuple(HEADER_ALIASES[label] for piece in re.split(r"[/|]", normalized_label(value))
                 if (label := piece.strip(" .:")) in HEADER_ALIASES)


def row_lines(words):
    lines = []
    for word in sorted(words, key=lambda word: (word["top"], word["x0"])):
        if not lines or word["top"] - lines[-1][0]["top"] > 3:
            lines.append([])
        lines[-1].append(word)
    return [sorted(line, key=lambda word: word["x0"]) for line in lines]


def needs_ocr(page, words):
    """Find a missing text layer, including outlined text and watermark-only pages."""
    if sum(page.height * 0.08 <= word["top"] <= page.height * 0.92 for word in words) >= 12:
        return False
    visible_shapes = len(page.curves) >= 80 or len(page.rects) >= 80
    raster_content = any(max(0, image["x1"] - image["x0"]) * max(0, image["bottom"] - image["top"])
                         >= page.width * page.height * 0.30 for image in page.images)
    return visible_shapes or raster_content


def ocr_languages():
    value = os.environ.get("SSPLAYER_OCR_LANGUAGES", "eng")
    if not re.fullmatch(r"[a-zA-Z0-9_]+(?:\+[a-zA-Z0-9_]+)*", value):
        raise ValueError("Use Tesseract language names such as eng+chi_sim for SSPLAYER_OCR_LANGUAGES.")
    return value


def read_ocr_words(data, number, width, height):
    import pypdfium2

    with pypdfium2.PdfDocument(data) as document, closing(document[number - 1]) as page:
        with closing(page.render(scale=3)) as bitmap:
            picture = BytesIO()
            bitmap.to_pil().save(picture, format="PNG")
    output = subprocess.run(("tesseract", "stdin", "stdout", "-l", ocr_languages(), "--psm", "11", "tsv"),
                            input=picture.getvalue(), capture_output=True, check=True, timeout=35).stdout.decode("utf-8")
    rows = list(csv.DictReader(StringIO(output), delimiter="\t"))
    if not rows:
        return []
    x_scale = width / max(1, float(rows[0]["width"]))
    y_scale = height / max(1, float(rows[0]["height"]))
    result = []
    for row in rows:
        if row["level"] != "5" or float(row["conf"]) < 35 or not row["text"].strip():
            continue
        left, top = float(row["left"]), float(row["top"])
        result.append({"text": row["text"], "x0": left * x_scale, "top": top * y_scale,
                       "x1": (left + float(row["width"])) * x_scale,
                       "bottom": (top + float(row["height"])) * y_scale, "ocr": True})
    return result


# Reuse only observations of identical source bytes. This also works when QA
# has opened a temporary copy of the PDF extracted from a package.
_OCR_PAGES = OrderedDict()


class ScorePageReader:
    def __init__(self, source):
        self.source = source
        self.warnings = []
        self._source_data = None
        self._source_hash = None

    def words(self, page, number):
        words = page.extract_words()
        if not needs_ocr(page, words):
            return words
        if not shutil.which("tesseract"):
            self.warnings.append(f"原谱第 {number} 页缺少可用文字层。本机未安装 Tesseract，请打开原页核对。")
            return words
        if self._source_data is None:
            if hasattr(self.source, "read"):
                position = self.source.tell()
                try:
                    self.source.seek(0)
                    self._source_data = self.source.read()
                finally:
                    self.source.seek(position)
            else:
                self._source_data = Path(self.source).read_bytes()
            self._source_hash = sha256(self._source_data).hexdigest()
        key = (self._source_hash, number, page.width, page.height, ocr_languages())
        if key not in _OCR_PAGES:
            try:
                found = read_ocr_words(self._source_data, number, page.width, page.height)
            except (OSError, ValueError, subprocess.SubprocessError):
                found = []
            _OCR_PAGES[key] = found
            if len(_OCR_PAGES) > 96:
                _OCR_PAGES.popitem(last=False)
        found = _OCR_PAGES[key]
        if len(found) > len(words):
            return found
        self.warnings.append(f"原谱第 {number} 页 OCR 未取得可复核的文字，请打开原页核对。")
        return words


def export_score_pages(source, numbers, destination):
    """Render the original physical pages alongside positioned text observations."""
    import pdfplumber
    import pypdfium2

    data = source.read_bytes()
    reader = ScorePageReader(BytesIO(data))
    entries = []
    with pdfplumber.open(BytesIO(data)) as score, pypdfium2.PdfDocument(data) as picture:
        if any(number < 1 or number > len(score.pages) for number in numbers):
            raise ValueError(f"Choose physical pages from 1 to {len(score.pages)}.")
        destination.mkdir(parents=True)
        for number in numbers:
            page = score.pages[number - 1]
            stem = f"page-{number:04}"
            overview, observations = destination / f"{stem}.png", destination / f"{stem}.txt"
            words = reader.words(page, number)
            with closing(picture[number - 1]) as visible:
                scale = min(2.5, 2400 / max(visible.get_size()))
                with closing(visible.render(scale=scale)) as bitmap:
                    bitmap.to_pil().save(overview)
            lines = row_lines(words)
            text = "\n".join(f"y={line[0]['top']:.1f}  " + "  ".join(
                f"[x={word['x0']:.1f}] {word['text']}" for word in line) for line in lines)
            observations.write_text(text + ("\n" if text else ""), encoding="utf-8")
            entries.append({"page": number, "image": overview.name, "text": observations.name,
                            "textSource": ("ocr" if any(word.get("ocr") for word in words)
                                           else "pdfText" if words else "none")})
        evidence = {"sourceSHA256": sha256(data).hexdigest(), "pageCount": len(score.pages),
                    "pages": entries, "warnings": reader.warnings}
    index = destination / "index.json"
    index.write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return index


def numbered_ocr_heading(words, width, pattern, inline_pattern):
    """Find a numbered scan heading before an independent table or coaching label."""
    headings = []
    anchored = False
    for line in row_lines(words):
        if any(not word.get("ocr") for word in line):
            continue
        title = " ".join(word["text"] for word in line[1:])
        label = normalized_label(" ".join(word["text"] for word in line))
        headers = {role for word in line for role in header_labels(word["text"])}
        if (re.match(r"^(?:TRACK FOCUS|FOCO DEL TRACK|TECHNIQUE AND COACHING)\b", label)
                or {"MUSIC", "EXERCISE"}.issubset(headers)):
            anchored = True
            break
        if not (1 < len(line) and line[0]["x0"] < width * 0.25 and line[0]["top"] < width * 0.45):
            continue
        value = normalized_label(line[0]["text"])
        value = re.sub(r"^O(?=\d)", "0", value)
        if re.fullmatch(r"\d{1,2}", value) and title.isupper():
            value += "."
        if pattern.fullmatch(value) or inline_pattern.fullmatch(value):
            headings.append([{**line[0], "text": value}, *line[1:]])
    return headings[0] if anchored and len(headings) == 1 else None
