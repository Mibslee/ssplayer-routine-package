#!/usr/bin/env python3
"""Build a privacy-safe PDF cue to music/video time mapping.

The choreography PDF is the semantic timeline. Every PDF cue is a time on its
own track clock, not a time on the long-form video clock. Audio analysis is used
only to locate each complete music track inside a rehearsal video. The tool
never modifies source media and deliberately omits choreography text from its
JSON and Markdown outputs.

Example:

    python3 scripts/rehearsal_alignment.py \
      --pdf /path/to/BODYPUMP136.pdf \
      --video /path/to/BODYPUMP136.mp4 \
      --music-dir "/path/to/BODYPUMP136 Music" \
      --program bodypump --release 136 \
      --output-dir artifacts/qa/rehearsal-alignment/raw/bp136

Runtime dependencies: ffmpeg, ffprobe, numpy and pdfplumber.
"""

from __future__ import annotations

import argparse
import dataclasses
import datetime as dt
import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Iterable, Sequence

from score_evidence import header_labels, numbered_ocr_heading


SCHEMA_VERSION = 2
PDF_CUE_TIME_BASIS = "track-relative"
DEFAULT_TRACK_AUDIO_START_SECONDS = 0.0
MEDIA_FINGERPRINT_SAMPLE_SIZE = 65_536
MEDIA_FINGERPRINT_NAMESPACE = b"SSPLAYER_MEDIA_SAMPLE_V1\0"
DEFAULT_DECODE_RATE = 4_000
DEFAULT_FFT_SIZE = 1_024
DEFAULT_HOP_SIZE = 400
DEFAULT_BAND_COUNT = 32
DEFAULT_MIN_FREQUENCY = 45.0
DEFAULT_MAX_FREQUENCY = 1_900.0
SUPPORTED_AUDIO_SUFFIXES = {".aac", ".aif", ".aiff", ".flac", ".m4a", ".mp3", ".wav"}
STANDARD_TRACK_RE = re.compile(
    r"^(?P<number>\d{1,2})(?P<segment>[A-Z])?[\s._-]+(?P<title>.+)$",
    re.IGNORECASE,
)
VARIANT_TRACK_RE = re.compile(
    r"^(?P<variant>ALTERNATE|ALT|BONUS|BNS|A|B)[\s._-]*"
    r"(?P<number>\d{1,2})(?P<segment>[A-Z])?[\s._-]+(?P<title>.+)$",
    re.IGNORECASE,
)
INDEXED_VARIANT_TRACK_RE = re.compile(
    r"^(?P<catalog_index>\d{1,2})[\s._-]+"
    r"(?P<variant>ALTERNATE|ALT|BONUS|BNS)[\s._-]+"
    r"(?P<number>\d{1,2})(?P<segment>[A-Z])?[\s._-]+(?P<title>.+)$",
    re.IGNORECASE,
)
BP45_TRACK_RE = re.compile(
    r"^BP[\s._-]*45[\s._-]*(?:COMBO[\s._-]*)?"
    r"[\[(]?(?P<combo>[AB])[\])]?(?=$|[\s._-])"
    r"(?:[\s._-]+(?P<title>.+))?$",
    re.IGNORECASE,
)
PDF_TRACK_RE = re.compile(
    r"^(?P<number>\d{1,2})(?:(?P<segment>[A-Z])\.?|\.)$",
    re.IGNORECASE,
)
PDF_INLINE_TRACK_RE = re.compile(
    r"^(?P<number>\d{1,2})(?P<segment>[A-Z])?\.(?P<label>\S.+)$",
    re.IGNORECASE,
)
TIMESTAMP_RE = re.compile(r"^(?P<minutes>\d{1,2}):(?P<seconds>\d{2})$")


class AlignmentError(RuntimeError):
    """A user-actionable alignment failure."""


@dataclasses.dataclass(frozen=True)
class CuePage:
    page_number: int
    track_number: int
    variant: str
    cue_times_seconds: tuple[float, ...]
    segment: str | None = None
    variant_source: str = dataclasses.field(default="default", repr=False)


@dataclasses.dataclass(frozen=True)
class PDFTrackTimeline:
    track_number: int
    variant: str
    pages: tuple[int, ...]
    cue_times_seconds: tuple[float, ...]
    segment: str | None = None
    variant_source: str = "default"
    technique_and_coaching_pages: tuple[int, ...] = ()


@dataclasses.dataclass(frozen=True)
class MusicTrack:
    track_number: int
    path: Path
    duration_seconds: float
    segment: str | None = None
    variant: str = "standard"


@dataclasses.dataclass(frozen=True)
class AlignmentPeak:
    offset_frames: int
    score: float
    second_offset_frames: int | None
    second_score: float | None

    @property
    def score_gap(self) -> float | None:
        if self.second_score is None:
            return None
        return self.score - self.second_score


@dataclasses.dataclass
class TrackAlignment:
    track_number: int
    music_file_name: str
    music_content_fingerprint: str
    music_duration_seconds: float
    track_audio_start_seconds: float
    pdf_timeline: PDFTrackTimeline | None
    spectral_peak: AlignmentPeak
    video_start_seconds: float
    raw_refined_start_seconds: float | None
    raw_refinement_score: float | None
    confidence: str
    status: str
    warnings: list[str]
    segment: str | None = None
    variant: str = "standard"


@dataclasses.dataclass(frozen=True)
class CueTimeProjection:
    """A PDF cue projected from the track clock into audio/video clocks."""

    pdf_track_time_seconds: float
    projected_audio_cue_time_seconds: float
    projected_video_cue_time_seconds: float | None


def _require_modules() -> tuple[Any, Any]:
    try:
        import numpy as np
    except ImportError as error:
        raise AlignmentError(
            "Missing Python dependency 'numpy'. Use the Codex bundled Python runtime "
            "or install it with: python3 -m pip install numpy"
        ) from error
    try:
        import pdfplumber
    except ImportError as error:
        raise AlignmentError(
            "Missing Python dependency 'pdfplumber'. Install it with: "
            "python3 -m pip install pdfplumber"
        ) from error
    return np, pdfplumber


def sampled_content_fingerprint(path: Path) -> str:
    """Return the bounded-cost media identity also used by the iOS app."""

    size = path.stat().st_size
    last_offset = max(0, size - MEDIA_FINGERPRINT_SAMPLE_SIZE)
    offsets = sorted({0, last_offset // 3, (last_offset * 2) // 3, last_offset})
    digest = hashlib.sha256()
    digest.update(MEDIA_FINGERPRINT_NAMESPACE)
    digest.update(str(size).encode("ascii"))
    digest.update(b"\0")
    with path.open("rb") as handle:
        for offset in offsets:
            handle.seek(offset)
            data = handle.read(MEDIA_FINGERPRINT_SAMPLE_SIZE)
            digest.update(str(offset).encode("ascii"))
            digest.update(b"\0")
            digest.update(str(len(data)).encode("ascii"))
            digest.update(b"\0")
            digest.update(data)
    return digest.hexdigest()


def check_dependencies(ffmpeg: str, ffprobe: str) -> None:
    _require_modules()
    for executable, label in ((ffmpeg, "ffmpeg"), (ffprobe, "ffprobe")):
        if shutil.which(executable) is None and not Path(executable).is_file():
            raise AlignmentError(
                f"Missing {label} executable: {executable!r}. On macOS install it with "
                "'brew install ffmpeg' or pass the executable path explicitly."
            )


def parse_timestamp(value: str) -> float | None:
    match = TIMESTAMP_RE.fullmatch(value.strip())
    if not match:
        return None
    seconds = int(match.group("seconds"))
    if seconds >= 60:
        return None
    return float(int(match.group("minutes")) * 60 + seconds)


def _word_text(word: dict[str, Any]) -> str:
    return str(word.get("text", "")).strip()


def _normalized_segment(value: str | None) -> str | None:
    if value is None:
        return None
    segment = value.strip().upper()
    return segment or None


def _music_variant(prefix: str | None) -> str:
    normalized = (prefix or "").strip().upper()
    if normalized in {"A", "ALT", "ALTERNATE"}:
        return "alternate"
    if normalized in {"B", "BNS", "BONUS"}:
        return "bonus"
    return "standard"


def _fulfillment_id(track_number: int, segment: str | None) -> str:
    return f"T{track_number:02d}{_normalized_segment(segment) or ''}"


def _timeline_fulfillment_id(
    track_number: int,
    segment: str | None,
    variant: str,
) -> str:
    base = _fulfillment_id(track_number, segment)
    return base if variant == "standard" else f"{base}.{variant}"


def _classify_pdf_variant_with_source(
    words: Sequence[dict[str, Any]],
    page_width: float,
) -> tuple[str, str]:
    """Classify a cue table without trusting a clock reset as a variant label.

    Newer booklets print ``BONUS``/``ALT`` explicitly. Several older BP
    booklets instead place the marker on the track heading row (around y=98),
    use ``SECTION - replacement title`` for alternate choreography, or use a
    combined ``TRICEPS/BICEPS`` heading for the 45-minute table. Those
    structural forms are kept separate but marked as inferred so downstream
    alignment remains review-required.
    """

    heading_words = sorted(
        (
            word
            for word in words
            if 60 <= float(word.get("top", 10_000)) <= 125
            and 0 <= float(word.get("x0", page_width)) < page_width * 0.65
        ),
        key=lambda word: (
            round(float(word.get("top", 0)) / 4),
            float(word.get("x0", 0)),
        ),
    )
    marker_words = {_word_text(word).upper() for word in heading_words}
    if "BONUS" in marker_words:
        return "bonus", "explicit"
    if marker_words & {"ALT", "ALTERNATE"}:
        return "alternate", "explicit"
    if any(marker.startswith("45-MIN") for marker in marker_words):
        return "45-minute", "explicit"

    # Do not mistake BODYCOMBAT's ``05A / 05B.`` numeric heading for an
    # express-format label. The slash must be embedded in a descriptive word.
    has_combined_section_label = any(
        "/" in _word_text(word)
        and _word_text(word) != "/"
        and PDF_TRACK_RE.fullmatch(_word_text(word)) is None
        and (
            PDF_INLINE_TRACK_RE.fullmatch(_word_text(word)) is None
            or "/" in (PDF_INLINE_TRACK_RE.fullmatch(_word_text(word)).group("label"))
        )
        for word in heading_words
    )
    if has_combined_section_label:
        return "45-minute", "structured"

    bp_section_labels = {
        "WARMUP",
        "WARM-UP",
        "SQUATS",
        "CHEST",
        "BACK",
        "TRICEPS",
        "BICEPS",
        "LUNGES",
        "SHOULDERS",
        "CORE",
        "COOLDOWN",
    }
    if "-" in marker_words and marker_words & bp_section_labels:
        return "alternate", "structured"
    return "standard", "default"


def classify_pdf_variant(words: Sequence[dict[str, Any]], page_width: float = 566.929) -> str:
    return _classify_pdf_variant_with_source(words, page_width)[0]


def _has_cue_table_header(
    page_width: float,
    words: Sequence[dict[str, Any]],
) -> bool:
    """Recognize the positioned choreography table header, not body copy.

    BODYPUMP commonly prints ``SEQUENCE/EXERCISE`` as one header while
    BODYCOMBAT uses a standalone ``EXERCISE`` column. RPM uses a three-part
    ``MUSIC`` / ``POSITION ...`` / ``PACE`` signature. Requiring the relevant
    positioned headers on the same row avoids accepting pages merely because
    their choreography prose contains words such as "sequence" or "position".
    """

    music_headers = [
        word
        for word in words
        if "MUSIC" in header_labels(_word_text(word))
        and 0 <= float(word.get("x0", page_width)) < page_width * 0.40
    ]
    exercise_headers = [
        word
        for word in words
        if "EXERCISE" in header_labels(_word_text(word))
        and page_width * 0.20 < float(word.get("x0", -1)) <= page_width
    ]
    exercise_table = any(
        abs(float(music.get("top", -1)) - float(exercise.get("top", -10))) <= 6
        and float(exercise.get("x0", 0)) > float(music.get("x0", page_width))
        for music in music_headers
        for exercise in exercise_headers
    )
    if exercise_table:
        return True

    position_headers = [
        word
        for word in words
        if _word_text(word).upper() == "POSITION"
        and page_width * 0.20 < float(word.get("x0", -1)) < page_width * 0.80
    ]
    pace_headers = [
        word
        for word in words
        if _word_text(word).upper() == "PACE"
        and page_width * 0.60 < float(word.get("x0", -1)) <= page_width
    ]
    return any(
        max(
            float(music.get("top", -1)),
            float(position.get("top", -10)),
            float(pace.get("top", -20)),
        )
        - min(
            float(music.get("top", -1)),
            float(position.get("top", -10)),
            float(pace.get("top", -20)),
        )
        <= 6
        and float(music.get("x0", page_width))
        < float(position.get("x0", -1))
        < float(pace.get("x0", -1))
        for music in music_headers
        for position in position_headers
        for pace in pace_headers
    )


def _has_modern_technique_and_coaching_heading(
    words: Sequence[dict[str, Any]],
) -> bool:
    """Accept only the explicit modern heading, never a legacy guess.

    Early releases use substantially different structures such as
    ``TECHNIQUE & COACHING LAYER 1`` or program-specific block objectives.
    Those pages stay available in the original PDF but are intentionally not
    auto-attached to a music track.
    """

    ordered = sorted(
        words,
        key=lambda word: (
            round(float(word.get("top", 0)) / 4),
            float(word.get("x0", 0)),
        ),
    )
    normalized = " ".join(
        _collapse_doubled_glyph_word(_word_text(word).upper().strip())
        for word in ordered
    )
    return re.search(r"\bTECHNIQUE\s+AND\s+COACHING\b", normalized) is not None


def _collapse_doubled_glyph_word(word: str) -> str:
    """Repair a duplicated PDF text-layer word without broad fuzzy matching."""

    if not word or len(word) % 2:
        return word
    pairs = [word[index : index + 2] for index in range(0, len(word), 2)]
    if not all(pair[0] == pair[1] for pair in pairs):
        return word
    return "".join(pair[0] for pair in pairs)


def parse_cue_page_words(
    page_number: int,
    page_width: float,
    words: Sequence[dict[str, Any]],
) -> CuePage | None:
    """Parse privacy-safe page metadata from positioned PDF words.

    Only page number, track/variant and timestamp anchors are retained. Text in
    the music, exercise, repetitions and coaching columns never leaves the PDF.
    """

    if not _has_cue_table_header(page_width, words):
        return None

    track_candidates: list[tuple[float, float, int, str | None]] = []
    for word in words:
        top = float(word.get("top", 10_000))
        x0 = float(word.get("x0", page_width))
        text = _word_text(word)
        match = PDF_TRACK_RE.fullmatch(text) or PDF_INLINE_TRACK_RE.fullmatch(text)
        if 85 <= top <= 125 and 0 <= x0 < page_width * 0.25 and match:
            track_candidates.append((
                top,
                x0,
                int(match.group("number")),
                _normalized_segment(match.group("segment")),
            ))
    if not track_candidates:
        heading = numbered_ocr_heading(words, page_width, PDF_TRACK_RE, PDF_INLINE_TRACK_RE)
        if not heading:
            return None
        word = heading[0]
        match = PDF_TRACK_RE.fullmatch(word["text"]) or PDF_INLINE_TRACK_RE.fullmatch(word["text"])
        track_candidates.append((word["top"], word["x0"], int(match.group("number")),
                                 _normalized_segment(match.group("segment"))))
    track_candidates.sort()
    track_numbers = {candidate[2] for candidate in track_candidates}
    if len(track_numbers) != 1:
        return None
    track_number = track_candidates[0][2]
    heading_segments = {
        candidate[3] for candidate in track_candidates if candidate[3] is not None
    }
    # A heading such as ``05A / 05B.`` explicitly describes one combined
    # table. A single lettered heading retains its segment for the later
    # continuous-clock resolver.
    segment = next(iter(heading_segments)) if len(heading_segments) == 1 else None

    time_column_limit = page_width * 0.20
    positioned_times: list[tuple[float, float]] = []
    for word in words:
        x0 = float(word.get("x0", page_width))
        if x0 > time_column_limit:
            continue
        seconds = parse_timestamp(_word_text(word))
        if seconds is None:
            continue
        positioned_times.append((float(word.get("top", 0)), seconds))
    if not positioned_times:
        return None

    positioned_times.sort()
    cue_times: list[float] = []
    for _, seconds in positioned_times:
        if not cue_times or not math.isclose(cue_times[-1], seconds, abs_tol=0.001):
            cue_times.append(seconds)
    variant, variant_source = _classify_pdf_variant_with_source(words, page_width)
    return CuePage(
        page_number=page_number,
        track_number=track_number,
        variant=variant,
        cue_times_seconds=tuple(cue_times),
        segment=segment,
        variant_source=variant_source,
    )


def _timeline_from_pages(
    track_number: int,
    variant: str,
    segment: str | None,
    pages: Sequence[CuePage],
    warnings: list[str],
    variant_source: str | None = None,
) -> PDFTrackTimeline:
    ordered_pages = sorted(pages, key=lambda item: item.page_number)
    cue_times: list[float] = []
    for page in ordered_pages:
        for seconds in page.cue_times_seconds:
            if cue_times and seconds < cue_times[-1] - 1:
                warnings.append(
                    f"{_fulfillment_id(track_number, segment)} ({variant}) cue clock "
                    f"moves backward on PDF page {page.page_number}; review the page manually."
                )
            cue_times.append(seconds)
    return PDFTrackTimeline(
        track_number=track_number,
        variant=variant,
        pages=tuple(page.page_number for page in ordered_pages),
        cue_times_seconds=tuple(cue_times),
        segment=segment,
        variant_source=variant_source
        or (
            next(iter({page.variant_source for page in ordered_pages}))
            if len({page.variant_source for page in ordered_pages}) == 1
            else "mixed"
        ),
    )


def _partition_clock_runs(pages: Sequence[CuePage]) -> list[list[CuePage]]:
    """Split only page-boundary clock resets; never guess their variant."""

    runs: list[list[CuePage]] = []
    current: list[CuePage] = []
    last_by_segment: dict[str | None, float] = {}
    for page in sorted(pages, key=lambda item: item.page_number):
        first = page.cue_times_seconds[0]
        previous_last = last_by_segment.get(page.segment)
        if previous_last is not None and first < previous_last - 1:
            runs.append(current)
            current = []
            last_by_segment.clear()
        current.append(page)
        last_by_segment[page.segment] = page.cue_times_seconds[-1]
    if current:
        runs.append(current)
    return runs


def _build_pdf_timelines(
    cue_pages: Sequence[CuePage],
) -> tuple[list[PDFTrackTimeline], list[str]]:
    grouped: dict[tuple[int, str], list[CuePage]] = {}
    for page in cue_pages:
        grouped.setdefault((page.track_number, page.variant), []).append(page)

    warnings: list[str] = []
    resolved_groups: list[tuple[int, str, list[CuePage], str | None]] = []
    for (track_number, variant), pages in sorted(grouped.items()):
        clock_runs = _partition_clock_runs(pages)
        resolved_groups.append((track_number, variant, clock_runs[0], None))
        for run in clock_runs[1:]:
            review_variant = f"review-required-p{run[0].page_number}"
            warnings.append(
                f"Track {track_number:02d} ({variant}) restarts its cue clock on PDF "
                f"page {run[0].page_number} without a reliable variant marker; preserved "
                f"as {review_variant} instead of merging or guessing."
            )
            resolved_groups.append(
                (track_number, review_variant, run, "clock-reset")
            )

    timelines: list[PDFTrackTimeline] = []
    for track_number, variant, pages, forced_variant_source in sorted(
        resolved_groups,
        key=lambda item: (item[0], item[1], item[2][0].page_number),
    ):
        ordered_pages = sorted(pages, key=lambda item: item.page_number)
        segments = {page.segment for page in ordered_pages}
        flattened_times = [
            seconds
            for page in ordered_pages
            for seconds in page.cue_times_seconds
        ]
        clock_is_continuous = all(
            current >= previous - 1
            for previous, current in zip(flattened_times, flattened_times[1:])
        )

        if segments == {None}:
            timelines.append(
                _timeline_from_pages(
                    track_number,
                    variant,
                    None,
                    ordered_pages,
                    warnings,
                    forced_variant_source,
                )
            )
            continue
        if None not in segments and len(segments) > 1 and clock_is_continuous:
            timelines.append(
                _timeline_from_pages(
                    track_number,
                    variant,
                    None,
                    ordered_pages,
                    warnings,
                    forced_variant_source,
                )
            )
            continue

        if len(segments) > 1:
            segment_labels = ", ".join(
                _fulfillment_id(track_number, segment)
                for segment in sorted(
                    segments,
                    key=lambda value: (value is None, value or ""),
                )
            )
            warnings.append(
                f"Track {track_number:02d} ({variant}) has separate or ambiguous "
                f"lettered cue clocks; preserved {segment_labels} instead of merging them."
            )

        pages_by_segment: dict[str | None, list[CuePage]] = {}
        for page in ordered_pages:
            pages_by_segment.setdefault(page.segment, []).append(page)
        for segment, segment_pages in sorted(
            pages_by_segment.items(),
            key=lambda item: (item[0] is None, item[0] or ""),
        ):
            timelines.append(
                _timeline_from_pages(
                    track_number,
                    variant,
                    segment,
                    segment_pages,
                    warnings,
                    forced_variant_source,
                )
            )
    return timelines, warnings


def _attach_technique_pages(
    timelines: Sequence[PDFTrackTimeline],
    cue_pages: Sequence[CuePage],
    technique_pages: Sequence[int],
) -> list[PDFTrackTimeline]:
    pages_by_timeline: dict[int, list[int]] = {}
    ordered_cues = sorted(cue_pages, key=lambda item: item.page_number)
    for technique_page in sorted(set(technique_pages)):
        preceding = [
            cue_page
            for cue_page in ordered_cues
            if cue_page.page_number < technique_page
        ]
        if not preceding:
            continue
        source_page = preceding[-1].page_number
        for timeline_index, timeline in enumerate(timelines):
            if source_page in timeline.pages:
                pages_by_timeline.setdefault(timeline_index, []).append(
                    technique_page
                )
                break
    return [
        dataclasses.replace(
            timeline,
            technique_and_coaching_pages=tuple(
                sorted(set(pages_by_timeline.get(index, [])))
            ),
        )
        for index, timeline in enumerate(timelines)
    ]


def extract_pdf_timelines(pdf_path: Path) -> tuple[list[PDFTrackTimeline], list[str]]:
    _, pdfplumber = _require_modules()
    if not pdf_path.is_file():
        raise AlignmentError(f"PDF not found: {pdf_path}")

    cue_pages: list[CuePage] = []
    technique_pages: list[int] = []
    with pdfplumber.open(pdf_path) as pdf:
        for index, page in enumerate(pdf.pages, start=1):
            words = page.extract_words() or []
            cue_page = parse_cue_page_words(index, float(page.width), words)
            if cue_page is not None:
                cue_pages.append(cue_page)
            if _has_modern_technique_and_coaching_heading(words):
                technique_pages.append(index)

    timelines, warnings = _build_pdf_timelines(cue_pages)
    if not timelines:
        raise AlignmentError(
            "No choreography cue tables were detected. The PDF needs a text layer with "
            "positioned MM:SS values in the left column; otherwise use manual cue import."
        )
    timelines = _attach_technique_pages(
        timelines,
        cue_pages,
        technique_pages,
    )
    return timelines, warnings


def _run_capture(command: Sequence[str], label: str) -> bytes:
    process = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
    if process.returncode != 0:
        diagnostic = process.stderr.decode("utf-8", errors="replace").strip().splitlines()
        detail = diagnostic[-1] if diagnostic else f"exit status {process.returncode}"
        raise AlignmentError(f"{label} failed: {detail}")
    return process.stdout


def media_duration(path: Path, ffprobe: str) -> float:
    if not path.is_file():
        raise AlignmentError(f"Media file not found: {path}")
    output = _run_capture(
        [
            ffprobe,
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            os.fspath(path),
        ],
        f"ffprobe for {path.name}",
    )
    try:
        duration = float(output.decode("ascii").strip())
    except ValueError as error:
        raise AlignmentError(f"ffprobe returned an invalid duration for {path.name}") from error
    if not math.isfinite(duration) or duration <= 0:
        raise AlignmentError(f"Media duration is invalid for {path.name}: {duration}")
    return duration


def _bp45_track_number(program: str | None, release: str | None, combo: str) -> int | None:
    """Return the release-owned BP45 combo track without guessing.

    Releases 132-138 catalogue Combo A as T5 and Combo B as T7. BP UNITED is
    the documented exception whose Combo B belongs to T6. A BP45-looking file
    outside that scope is rejected instead of being silently assigned to the
    wrong section.
    """

    normalized_program = (program or "auto").strip().casefold()
    if normalized_program not in {"auto", "bp", "bodypump"}:
        return None
    normalized_release = (release or "").strip().upper()
    release_supported = normalized_release == "UNITED" or (
        normalized_release.isdigit() and 132 <= int(normalized_release) <= 138
    )
    if not release_supported:
        raise AlignmentError(
            "BP45 audio requires --release 132 through 138 or UNITED so Combo B "
            "can be assigned without guessing."
        )
    if combo.upper() == "A":
        return 5
    return 6 if normalized_release == "UNITED" else 7


def scan_music_tracks(
    music_directory: Path,
    ffprobe: str,
    program: str | None = None,
    release: str | None = None,
) -> tuple[list[MusicTrack], list[str]]:
    if not music_directory.is_dir():
        raise AlignmentError(f"Music directory not found: {music_directory}")
    candidates: dict[tuple[int, str | None, str], Path] = {}
    warnings: list[str] = []
    for path in sorted(music_directory.iterdir(), key=lambda item: item.name.casefold()):
        if not path.is_file() or path.name.startswith("."):
            continue
        if ".ssplayer-cover-backup-" in path.name:
            continue
        if path.suffix.casefold() not in SUPPORTED_AUDIO_SUFFIXES:
            continue
        bp45_match = BP45_TRACK_RE.fullmatch(path.stem)
        if bp45_match is not None:
            number = _bp45_track_number(
                program,
                release,
                bp45_match.group("combo"),
            )
            if number is None:
                warnings.append(
                    f"Ignored BP45 audio outside a BODYPUMP import: {path.name}"
                )
                continue
            key = (number, None, "45-minute")
            if key in candidates:
                raise AlignmentError(
                    f"More than one music file claims "
                    f"{_timeline_fulfillment_id(number, None, '45-minute')}: "
                    f"{candidates[key].name!r} and {path.name!r}. Remove the ambiguity or "
                    "run the files separately."
                )
            candidates[key] = path
            continue
        variant_match = (
            INDEXED_VARIANT_TRACK_RE.fullmatch(path.stem)
            or VARIANT_TRACK_RE.fullmatch(path.stem)
        )
        match = variant_match or STANDARD_TRACK_RE.fullmatch(path.stem)
        if not match:
            warnings.append(f"Ignored non-standard or unnumbered audio file: {path.name}")
            continue
        number = int(match.group("number"))
        segment = _normalized_segment(match.group("segment"))
        variant = _music_variant(
            variant_match.group("variant") if variant_match is not None else None
        )
        key = (number, segment, variant)
        if key in candidates:
            raise AlignmentError(
                f"More than one music file claims "
                f"{_timeline_fulfillment_id(number, segment, variant)}: "
                f"{candidates[key].name!r} and {path.name!r}. Remove the ambiguity or "
                "run the files separately."
            )
        candidates[key] = path

    if not candidates:
        raise AlignmentError(
            "No standard numbered music files were found. Expected names such as "
            "'01 Track Name.mp3'."
        )

    keys_by_number: dict[tuple[int, str], set[str | None]] = {}
    for number, segment, variant in candidates:
        keys_by_number.setdefault((number, variant), set()).add(segment)
    for (number, variant), segments in sorted(keys_by_number.items()):
        if None in segments and len(segments) > 1:
            raise AlignmentError(
                f"Track {number:02d} ({variant}) mixes one combined music file with lettered "
                "segment files. Keep either Txx or TxxA/TxxB so binding stays explicit."
            )

    tracks = [
        MusicTrack(number, path, media_duration(path, ffprobe), segment, variant)
        for (number, segment, variant), path in sorted(
            candidates.items(),
            key=lambda item: (item[0][0], item[0][2], item[0][1] or ""),
        )
    ]
    return tracks, warnings


def decode_mono_float(path: Path, ffmpeg: str, sample_rate: int, filters: str | None = None) -> Any:
    np, _ = _require_modules()
    command = [ffmpeg, "-v", "error", "-i", os.fspath(path), "-map", "0:a:0", "-ac", "1"]
    if filters:
        command.extend(["-af", filters])
    command.extend(["-ar", str(sample_rate), "-f", "f32le", "pipe:1"])
    raw = _run_capture(command, f"ffmpeg audio decode for {path.name}")
    samples = np.frombuffer(raw, dtype="<f4")
    if samples.size < sample_rate:
        raise AlignmentError(f"Decoded audio is unexpectedly short for {path.name}")
    return samples.copy()


def spectral_features(
    samples: Any,
    sample_rate: int = DEFAULT_DECODE_RATE,
    fft_size: int = DEFAULT_FFT_SIZE,
    hop_size: int = DEFAULT_HOP_SIZE,
    band_count: int = DEFAULT_BAND_COUNT,
    min_frequency: float = DEFAULT_MIN_FREQUENCY,
    max_frequency: float = DEFAULT_MAX_FREQUENCY,
    batch_size: int = 2_048,
) -> Any:
    """Return phase-insensitive, per-frame normalized log-band features."""

    np, _ = _require_modules()
    signal = np.asarray(samples, dtype=np.float32)
    if signal.ndim != 1 or signal.size < fft_size:
        raise AlignmentError("Audio must contain at least one complete analysis frame.")
    if not 0 < min_frequency < max_frequency < sample_rate / 2:
        raise AlignmentError("Spectral frequency bounds must fit below the Nyquist frequency.")

    frame_count = 1 + (signal.size - fft_size) // hop_size
    window = np.hanning(fft_size).astype(np.float32)
    frequencies = np.fft.rfftfreq(fft_size, 1 / sample_rate)
    edges = np.geomspace(min_frequency, max_frequency, band_count + 1)
    ranges: list[tuple[int, int]] = []
    for low, high in zip(edges[:-1], edges[1:]):
        start = int(np.searchsorted(frequencies, low, side="left"))
        end = int(np.searchsorted(frequencies, high, side="right"))
        end = max(end, start + 1)
        ranges.append((start, min(end, frequencies.size)))

    features = np.empty((frame_count, band_count), dtype=np.float32)
    sample_offsets = np.arange(fft_size, dtype=np.int64)
    for first in range(0, frame_count, batch_size):
        last = min(first + batch_size, frame_count)
        frame_starts = np.arange(first, last, dtype=np.int64) * hop_size
        frames = signal[frame_starts[:, None] + sample_offsets[None, :]] * window
        magnitudes = np.abs(np.fft.rfft(frames, axis=1))
        band_features = np.empty((last - first, band_count), dtype=np.float32)
        for band, (start, end) in enumerate(ranges):
            band_features[:, band] = np.log(
                np.maximum(magnitudes[:, start:end].mean(axis=1), 1e-7)
            )
        band_features -= band_features.mean(axis=1, keepdims=True)
        norms = np.linalg.norm(band_features, axis=1, keepdims=True)
        band_features /= np.maximum(norms, 1e-7)
        features[first:last] = band_features
    return features


def _next_power_of_two(value: int) -> int:
    return 1 << max(0, value - 1).bit_length()


def feature_correlation(video_features: Any, query_features: Any) -> Any:
    """Cross-correlate normalized spectral feature matrices over time."""

    np, _ = _require_modules()
    video = np.asarray(video_features, dtype=np.float32)
    query = np.asarray(query_features, dtype=np.float32)
    if video.ndim != 2 or query.ndim != 2 or video.shape[1] != query.shape[1]:
        raise AlignmentError("Video and query features must have the same band count.")
    if query.shape[0] > video.shape[0]:
        raise AlignmentError("Music track is longer than the decoded video audio.")

    transform_size = _next_power_of_two(video.shape[0] + query.shape[0] - 1)
    video_fft = np.fft.rfft(video, n=transform_size, axis=0)
    query_fft = np.fft.rfft(query[::-1], n=transform_size, axis=0)
    convolution = np.fft.irfft(video_fft * query_fft, n=transform_size, axis=0)
    full_scores = convolution.sum(axis=1) / query.shape[0]
    return full_scores[query.shape[0] - 1 : video.shape[0]].astype(np.float32)


def best_alignment_peak(
    scores: Any,
    exclusion_frames: int,
    start_frame: int = 0,
    end_frame: int | None = None,
) -> AlignmentPeak:
    np, _ = _require_modules()
    values = np.asarray(scores, dtype=np.float32)
    if values.ndim != 1 or values.size == 0:
        raise AlignmentError("Alignment score series is empty.")
    lower = max(0, start_frame)
    upper = values.size if end_frame is None else min(values.size, end_frame)
    if lower >= upper:
        raise AlignmentError("Alignment search window is empty.")
    local_index = int(np.argmax(values[lower:upper]))
    best_index = lower + local_index
    best_score = float(values[best_index])

    masked = values[lower:upper].copy()
    mask_start = max(0, local_index - exclusion_frames)
    mask_end = min(masked.size, local_index + exclusion_frames + 1)
    masked[mask_start:mask_end] = -np.inf
    if np.isfinite(masked).any():
        second_local = int(np.argmax(masked))
        second_index: int | None = lower + second_local
        second_score: float | None = float(masked[second_local])
    else:
        second_index = None
        second_score = None
    return AlignmentPeak(best_index, best_score, second_index, second_score)


def normalized_waveform_correlation(video_samples: Any, query_samples: Any) -> Any:
    """Normalized raw-waveform correlation used only for local sub-frame refinement."""

    np, _ = _require_modules()
    video = np.asarray(video_samples, dtype=np.float64)
    query = np.asarray(query_samples, dtype=np.float64)
    if query.size > video.size:
        raise AlignmentError("Raw refinement query is longer than its video window.")
    query = query - query.mean()
    query_energy = float(np.dot(query, query))
    if query_energy <= 1e-12:
        raise AlignmentError("Raw refinement query is silent.")

    transform_size = _next_power_of_two(video.size + query.size - 1)
    numerator = np.fft.irfft(
        np.fft.rfft(video, transform_size) * np.fft.rfft(query[::-1], transform_size),
        transform_size,
    )[query.size - 1 : video.size]
    prefix_sum = np.concatenate(([0.0], np.cumsum(video)))
    prefix_square = np.concatenate(([0.0], np.cumsum(video * video)))
    window_sum = prefix_sum[query.size :] - prefix_sum[: -query.size]
    window_square = prefix_square[query.size :] - prefix_square[: -query.size]
    window_energy = np.maximum(window_square - window_sum * window_sum / query.size, 1e-12)
    return (numerator / np.sqrt(query_energy * window_energy)).astype(np.float32)


def classify_confidence(peak: AlignmentPeak) -> tuple[str, str]:
    gap = peak.score_gap if peak.score_gap is not None else 0.0
    if peak.score >= 0.45 and gap >= 0.08:
        return "high", "matched"
    if peak.score >= 0.32 and gap >= 0.05:
        return "medium", "matched"
    if peak.score >= 0.22:
        return "low", "ambiguous"
    return "insufficient", "unmatched"


def _timeline_map(
    timelines: Iterable[PDFTrackTimeline],
) -> dict[tuple[int, str | None, str], PDFTrackTimeline]:
    result: dict[tuple[int, str | None, str], PDFTrackTimeline] = {}
    for timeline in timelines:
        key = (timeline.track_number, timeline.segment, timeline.variant)
        if key in result:
            raise AlignmentError(
                f"More than one PDF timeline claims "
                f"{_timeline_fulfillment_id(timeline.track_number, timeline.segment, timeline.variant)}."
            )
        result[key] = timeline
    return result


def _timeline_for_music_track(
    track: MusicTrack,
    timeline_by_track: dict[tuple[int, str | None, str], PDFTrackTimeline],
) -> tuple[PDFTrackTimeline | None, str | None, bool]:
    """Choose an exact PDF variant, with one narrow review-only fallback.

    Older BP releases sometimes name express-format replacement audio ``BNS``
    while the booklet labels the same cue table as a 45-minute combined
    section. That is the only cross-variant fallback allowed. It is surfaced as
    review-required rather than silently certifying the pairing.
    """

    exact = timeline_by_track.get(
        (track.track_number, track.segment, track.variant)
    )
    if exact is not None:
        inferred_nonstandard = (
            track.variant != "standard"
            and exact.variant_source not in {"explicit", "default"}
        )
        warning = None
        if inferred_nonstandard:
            warning = (
                f"The PDF {track.variant} label for "
                f"{_fulfillment_id(track.track_number, track.segment)} was inferred from "
                "the heading structure; review it before certification."
            )
        return exact, warning, inferred_nonstandard

    if track.variant == "bonus":
        express = timeline_by_track.get(
            (track.track_number, track.segment, "45-minute")
        )
        if express is not None:
            return (
                express,
                (
                    f"Mapped {_timeline_fulfillment_id(track.track_number, track.segment, track.variant)} "
                    "to the release's sole 45-minute combined cue table; review the "
                    "replacement pairing before certification."
                ),
                True,
            )
    return None, None, False


def _validate_lettered_media_shape(
    tracks: Sequence[MusicTrack],
    timelines: Sequence[PDFTrackTimeline],
) -> None:
    music_segments: dict[tuple[int, str], set[str | None]] = {}
    for track in tracks:
        music_segments.setdefault((track.track_number, track.variant), set()).add(
            track.segment
        )
    pdf_segments: dict[tuple[int, str], set[str | None]] = {}
    for timeline in timelines:
        pdf_segments.setdefault((timeline.track_number, timeline.variant), set()).add(
            timeline.segment
        )

    for number, variant in sorted(set(music_segments) | set(pdf_segments)):
        music = music_segments.get((number, variant), set())
        pdf = pdf_segments.get((number, variant), set())
        # It is valid for a release PDF to contain an optional replacement
        # timeline whose audio was not imported, or for the folder to contain
        # an optional track whose PDF page is absent. Only compare lettered
        # shapes when both sides claim this exact number and variant.
        if not music or not pdf:
            continue
        if not any(segment is not None for segment in music | pdf):
            continue
        if music != pdf:
            music_label = ", ".join(
                _fulfillment_id(number, segment)
                for segment in sorted(music, key=lambda value: value or "")
            ) or "none"
            pdf_label = ", ".join(
                _fulfillment_id(number, segment)
                for segment in sorted(pdf, key=lambda value: value or "")
            ) or "none"
            raise AlignmentError(
                f"Track {number:02d} ({variant}) has incompatible lettered media: music "
                f"[{music_label}], PDF [{pdf_label}]. Choose matching combined or "
                "matching segmented material before alignment."
            )


def align_tracks(
    video_path: Path,
    tracks: Sequence[MusicTrack],
    timelines: Sequence[PDFTrackTimeline],
    ffmpeg: str,
    sample_rate: int,
    fft_size: int,
    hop_size: int,
    band_count: int,
    refine: bool,
) -> tuple[list[TrackAlignment], float]:
    np, _ = _require_modules()
    _validate_lettered_media_shape(tracks, timelines)
    video_samples = decode_mono_float(video_path, ffmpeg, sample_rate)
    video_duration = video_samples.size / sample_rate
    video_features = spectral_features(
        video_samples,
        sample_rate=sample_rate,
        fft_size=fft_size,
        hop_size=hop_size,
        band_count=band_count,
    )
    del video_samples

    frame_seconds = hop_size / sample_rate
    timeline_by_track = _timeline_map(timelines)
    alignments: list[TrackAlignment] = []

    raw_video = None
    raw_rate = 400
    if refine:
        raw_video = decode_mono_float(
            video_path,
            ffmpeg,
            raw_rate,
            filters="highpass=f=40,lowpass=f=180",
        )

    for track in tracks:
        query_samples = decode_mono_float(track.path, ffmpeg, sample_rate)
        query_features = spectral_features(
            query_samples,
            sample_rate=sample_rate,
            fft_size=fft_size,
            hop_size=hop_size,
            band_count=band_count,
        )
        scores = feature_correlation(video_features, query_features)
        peak = best_alignment_peak(scores, exclusion_frames=max(1, round(20 / frame_seconds)))
        spectral_start = peak.offset_frames * frame_seconds
        confidence, status = classify_confidence(peak)
        warnings: list[str] = []

        timeline, timeline_warning, timeline_requires_review = _timeline_for_music_track(
            track,
            timeline_by_track,
        )
        if timeline_warning is not None:
            warnings.append(timeline_warning)
        if timeline_requires_review and status == "matched":
            status = "ambiguous"
            confidence = "low"
        if timeline is None:
            warnings.append(
                f"No matching PDF cue table was detected for "
                f"{_timeline_fulfillment_id(track.track_number, track.segment, track.variant)}."
            )
        elif timeline.cue_times_seconds and timeline.cue_times_seconds[-1] > track.duration_seconds + 2:
            warnings.append("The last PDF cue is beyond the music file duration; review the PDF/music pairing.")

        refined_start: float | None = None
        refinement_score: float | None = None
        if refine and raw_video is not None and status != "unmatched":
            raw_query = decode_mono_float(
                track.path,
                ffmpeg,
                raw_rate,
                filters="highpass=f=40,lowpass=f=180",
            )
            radius = 2.5
            window_start = max(0.0, spectral_start - radius)
            window_end = min(video_duration, spectral_start + track.duration_seconds + radius)
            first_sample = int(window_start * raw_rate)
            last_sample = int(window_end * raw_rate)
            local_scores = normalized_waveform_correlation(raw_video[first_sample:last_sample], raw_query)
            local_index = int(np.argmax(local_scores))
            refinement_score = float(local_scores[local_index])
            candidate = window_start + local_index / raw_rate
            if refinement_score >= 0.15 and abs(candidate - spectral_start) <= radius:
                refined_start = candidate
            else:
                warnings.append(
                    "Phase-sensitive refinement was weak; kept the phase-insensitive spectral offset."
                )

        final_start = refined_start if refined_start is not None else spectral_start
        alignments.append(
            TrackAlignment(
                track_number=track.track_number,
                music_file_name=track.path.name,
                music_content_fingerprint=sampled_content_fingerprint(track.path),
                music_duration_seconds=track.duration_seconds,
                track_audio_start_seconds=DEFAULT_TRACK_AUDIO_START_SECONDS,
                pdf_timeline=timeline,
                spectral_peak=peak,
                video_start_seconds=final_start,
                raw_refined_start_seconds=refined_start,
                raw_refinement_score=refinement_score,
                confidence=confidence,
                status=status,
                warnings=warnings,
                segment=track.segment,
                variant=track.variant,
            )
        )
        del query_samples, query_features, scores

    _apply_sequence_checks(alignments)
    return alignments, video_duration


def certify_program_alignments(alignments: Sequence[TrackAlignment], program: str) -> None:
    """BJ rhythm reuse makes medium peaks candidates, not automatic video bindings.

    Keep the raw scores/offsets for review; never resolve a repeated track or an
    alternate song merely because one medium peak ranks first.
    """
    if program != "bodyjam":
        return
    for alignment in alignments:
        if alignment.status == "matched" and getattr(alignment, "variant", "standard") != "standard":
            alignment.status = "ambiguous"
            alignment.warnings.append("BODYJAM ALT needs independently verified variant video; shared backing audio is not sufficient.")
        if alignment.status == "matched" and alignment.confidence != "high":
            alignment.status = "ambiguous"
            alignment.warnings.append("BODYJAM automatic video binding requires high confidence; review this candidate.")


def _apply_sequence_checks(alignments: list[TrackAlignment], allowed_overlap_seconds: float = 8.0) -> None:
    previous: TrackAlignment | None = None
    for alignment in sorted(
        (item for item in alignments if item.variant == "standard"),
        key=lambda item: (item.track_number, item.segment or ""),
    ):
        if alignment.status == "unmatched":
            continue
        if previous is not None:
            previous_end = previous.video_start_seconds + previous.music_duration_seconds
            if alignment.video_start_seconds < previous.video_start_seconds:
                alignment.warnings.append("The independent match violates track order.")
                alignment.status = "ambiguous"
                alignment.confidence = "low"
            elif alignment.video_start_seconds < previous_end - allowed_overlap_seconds:
                alignment.warnings.append("The independent matches overlap more than expected.")
                alignment.status = "ambiguous"
                alignment.confidence = "low"
        previous = alignment


def _round_seconds(value: float | None) -> float | None:
    return None if value is None else round(value, 3)


def project_track_relative_cue(
    pdf_track_time_seconds: float,
    track_audio_start_seconds: float,
    track_video_start_seconds: float | None,
    time_scale: float = 1.0,
) -> CueTimeProjection:
    """Project one track-relative PDF timestamp into its media clocks.

    The PDF clock restarts at zero for every track. ``track_audio_start_seconds``
    is the point in the standalone audio file that corresponds to PDF track
    time zero (normally 0). ``track_video_start_seconds`` is the matching point
    in the long-form video. A missing video start deliberately produces no
    projected video time for ambiguous or unmatched tracks.
    """

    values = (pdf_track_time_seconds, track_audio_start_seconds, time_scale)
    if not all(math.isfinite(value) for value in values):
        raise AlignmentError("Cue projection values must be finite.")
    if pdf_track_time_seconds < 0 or track_audio_start_seconds < 0:
        raise AlignmentError("Cue and track-audio times must be non-negative.")
    if time_scale <= 0:
        raise AlignmentError("Cue projection time scale must be positive.")
    if track_video_start_seconds is not None:
        if not math.isfinite(track_video_start_seconds) or track_video_start_seconds < 0:
            raise AlignmentError("Track-video start must be finite and non-negative.")

    projected_audio = track_audio_start_seconds + pdf_track_time_seconds
    projected_video = None
    if track_video_start_seconds is not None:
        projected_video = track_video_start_seconds + (
            projected_audio - track_audio_start_seconds
        ) * time_scale
    return CueTimeProjection(
        pdf_track_time_seconds=pdf_track_time_seconds,
        projected_audio_cue_time_seconds=projected_audio,
        projected_video_cue_time_seconds=projected_video,
    )


def _format_clock(seconds: float | None) -> str:
    if seconds is None:
        return "-"
    minutes, remainder = divmod(seconds, 60)
    return f"{int(minutes):02d}:{remainder:06.3f}"


def _source_descriptor(
    path: Path,
    duration: float | None = None,
    includes_fingerprint: bool = False,
) -> dict[str, Any]:
    descriptor: dict[str, Any] = {"fileName": path.name}
    if duration is not None:
        descriptor["durationSeconds"] = round(duration, 3)
    if includes_fingerprint and path.is_file():
        descriptor["sampledContentFingerprint"] = sampled_content_fingerprint(path)
    return descriptor


def build_report_payload(
    args: argparse.Namespace,
    timelines: Sequence[PDFTrackTimeline],
    alignments: Sequence[TrackAlignment],
    video_duration: float,
    warnings: Sequence[str],
) -> dict[str, Any]:
    timeline_payload = []
    for timeline in timelines:
        timeline_payload.append(
            {
                "trackNumber": timeline.track_number,
                "variant": timeline.variant,
                "variantSource": timeline.variant_source,
                "fulfillmentID": _timeline_fulfillment_id(
                    timeline.track_number,
                    timeline.segment,
                    timeline.variant,
                ),
                "cueTimeBasis": PDF_CUE_TIME_BASIS,
                "pdfPages": list(timeline.pages),
                "techniqueAndCoachingPDFPages": list(
                    timeline.technique_and_coaching_pages
                ),
                "cueCount": len(timeline.cue_times_seconds),
                "firstCueTrackTimeSeconds": _round_seconds(timeline.cue_times_seconds[0])
                if timeline.cue_times_seconds
                else None,
                "lastCueTrackTimeSeconds": _round_seconds(timeline.cue_times_seconds[-1])
                if timeline.cue_times_seconds
                else None,
                "cueTrackTimesSeconds": [
                    _round_seconds(value) for value in timeline.cue_times_seconds
                ],
            }
        )

    alignment_payload = []
    for item in alignments:
        peak = item.spectral_peak
        frame_seconds = args.hop_size / args.sample_rate
        matched_video_start = item.video_start_seconds if item.status == "matched" else None
        cue_projections: list[dict[str, Any]] = []
        if item.pdf_timeline is not None:
            for index, cue in enumerate(item.pdf_timeline.cue_times_seconds, start=1):
                projection = project_track_relative_cue(
                    cue,
                    item.track_audio_start_seconds,
                    matched_video_start,
                )
                cue_projections.append({
                    "cueIndex": index,
                    "pdfTrackTimeSeconds": _round_seconds(projection.pdf_track_time_seconds),
                    "projectedAudioCueTimeSeconds": _round_seconds(
                        projection.projected_audio_cue_time_seconds
                    ),
                    "projectedVideoCueTimeSeconds": _round_seconds(
                        projection.projected_video_cue_time_seconds
                    ),
                })
        alignment_payload.append(
            {
                "trackNumber": item.track_number,
                "variant": item.variant,
                "pdfTimelineVariant": item.pdf_timeline.variant
                if item.pdf_timeline is not None
                else None,
                "fulfillmentID": _timeline_fulfillment_id(
                    item.track_number,
                    item.segment,
                    item.variant,
                ),
                "musicFileName": item.music_file_name,
                "musicContentFingerprint": item.music_content_fingerprint,
                "musicDurationSeconds": round(item.music_duration_seconds, 3),
                "status": item.status,
                "confidence": item.confidence,
                "trackAudioStartSeconds": _round_seconds(item.track_audio_start_seconds),
                "trackVideoStartSeconds": _round_seconds(matched_video_start),
                "timeModel": {
                    "pdfCueTimeBasis": PDF_CUE_TIME_BASIS,
                    "audioFormula": (
                        "projectedAudioCueTimeSeconds = trackAudioStartSeconds + "
                        "pdfTrackTimeSeconds"
                    ),
                    "videoFormula": (
                        "projectedVideoCueTimeSeconds = trackVideoStartSeconds + "
                        "(projectedAudioCueTimeSeconds - trackAudioStartSeconds) * timeScale"
                    ),
                    "timeScale": 1.0,
                },
                "spectralMatch": {
                    "bestCandidateTrackVideoStartSeconds": _round_seconds(
                        peak.offset_frames * frame_seconds
                    ),
                    "score": round(peak.score, 4),
                    "secondBestTrackVideoStartSeconds": _round_seconds(
                        peak.second_offset_frames * frame_seconds
                        if peak.second_offset_frames is not None
                        else None
                    ),
                    "secondBestScore": round(peak.second_score, 4)
                    if peak.second_score is not None
                    else None,
                    "scoreGap": round(peak.score_gap, 4) if peak.score_gap is not None else None,
                },
                "rawRefinement": {
                    "acceptedTrackVideoStartSeconds": _round_seconds(
                        item.raw_refined_start_seconds
                    ),
                    "score": round(item.raw_refinement_score, 4)
                    if item.raw_refinement_score is not None
                    else None,
                },
                "pdfCueCount": len(item.pdf_timeline.cue_times_seconds)
                if item.pdf_timeline is not None
                else 0,
                "cueMappings": cue_projections,
                "warnings": item.warnings,
            }
        )

    return {
        "schemaVersion": SCHEMA_VERSION,
        "generatedAt": dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat(),
        "program": {"family": args.program, "release": args.release},
        "privacy": {
            "containsSourceMedia": False,
            "containsChoreographyText": False,
            "sourcePathsRetained": False,
            "note": (
                "Only basenames, sampled verification fingerprints, page numbers, "
                "track-relative cue times and derived time mappings are recorded."
            ),
        },
        "source": {
            "pdf": _source_descriptor(args.pdf),
            "video": _source_descriptor(
                args.video,
                video_duration,
                includes_fingerprint=True,
            ),
            "musicDirectoryName": args.music_dir.name,
        },
        "analysis": {
            "method": "phase-insensitive-log-band-correlation-v1",
            "sampleRate": args.sample_rate,
            "fftSize": args.fft_size,
            "hopSize": args.hop_size,
            "frameSeconds": args.hop_size / args.sample_rate,
            "bandCount": args.band_count,
            "secondPeakExclusionSeconds": 20,
            "rawRefinementEnabled": not args.no_refine,
        },
        "pdfTimelines": timeline_payload,
        "trackAlignments": alignment_payload,
        "warnings": list(warnings),
        "manualFallback": {
            "requiredForStatuses": ["ambiguous", "unmatched"],
            "steps": [
                "Choose an unmistakable beat or drop in the music and the same moment in the video.",
                "Save trackAudioAnchorTimeSeconds and videoAnchorTimeSeconds as an anchor.",
                (
                    "Use trackVideoStartSeconds = videoAnchorTimeSeconds - "
                    "(trackAudioAnchorTimeSeconds - trackAudioStartSeconds) * timeScale."
                ),
                "Add a second late-track anchor if drift is visible; use piecewise or affine mapping only after review.",
            ],
        },
    }


def write_reports(payload: dict[str, Any], output_directory: Path) -> tuple[Path, Path]:
    output_directory.mkdir(parents=True, exist_ok=True)
    json_path = output_directory / "alignment.json"
    markdown_path = output_directory / "report.md"
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    lines = [
        "# Rehearsal PDF / media alignment",
        "",
        f"- Program: `{payload['program']['family']} {payload['program']['release']}`",
        f"- Video duration: `{_format_clock(payload['source']['video']['durationSeconds'])}`",
        f"- Method: `{payload['analysis']['method']}`",
        "- Privacy: no source media or choreography text is embedded in this report.",
        "",
        "| Fulfillment | Status | Confidence | Track audio start | Track video start | Score | Gap | PDF cues |",
        "|---|---|---|---:|---:|---:|---:|---:|",
    ]
    for item in payload["trackAlignments"]:
        spectral = item["spectralMatch"]
        lines.append(
            f"| {item['fulfillmentID']} | {item['status']} | {item['confidence']} | "
            f"{_format_clock(item['trackAudioStartSeconds'])} | "
            f"{_format_clock(item['trackVideoStartSeconds'])} | {spectral['score']:.4f} | "
            f"{spectral['scoreGap'] if spectral['scoreGap'] is not None else '-'} | "
            f"{item['pdfCueCount']} |"
        )
    track_warnings = [
        (item["fulfillmentID"], warning)
        for item in payload["trackAlignments"]
        for warning in item["warnings"]
    ]
    lines.extend(
        [
            "",
            "## Interpretation",
            "",
            "Each PDF timestamp is track-relative: its clock restarts at zero for every song.",
            "`projectedAudioCueTimeSeconds = trackAudioStartSeconds + pdfTrackTimeSeconds`.",
            "For matched tracks, `projectedVideoCueTimeSeconds = trackVideoStartSeconds + (projectedAudioCueTimeSeconds - trackAudioStartSeconds) * timeScale`.",
            "`ambiguous` and `unmatched` rows must be confirmed with a saved manual anchor before use.",
            "A weak phase-sensitive refinement does not invalidate a stronger phase-insensitive spectral match.",
            "",
            "## Manual fallback",
            "",
        ]
    )
    for index, step in enumerate(payload["manualFallback"]["steps"], start=1):
        lines.append(f"{index}. {step}")
    if payload["warnings"] or track_warnings:
        lines.extend(["", "## Warnings", ""])
        lines.extend(f"- {warning}" for warning in payload["warnings"])
        lines.extend(f"- {fulfillment_id}: {warning}" for fulfillment_id, warning in track_warnings)
    markdown_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return json_path, markdown_path


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Align local choreography PDF cue times with numbered music files and a complete video. "
            "Source media is read-only and choreography text is omitted from outputs."
        )
    )
    parser.add_argument("--pdf", type=Path, required=True)
    parser.add_argument("--video", type=Path, required=True)
    parser.add_argument("--music-dir", type=Path, required=True)
    parser.add_argument("--program", choices=("bodypump", "bodycombat", "rpm", "bodyjam", "auto"), default="auto")
    parser.add_argument("--release", default="unknown")
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--tracks", help="Comma-separated standard track numbers to analyze (default: all numbered files).")
    parser.add_argument("--ffmpeg", default=shutil.which("ffmpeg") or "ffmpeg")
    parser.add_argument("--ffprobe", default=shutil.which("ffprobe") or "ffprobe")
    parser.add_argument("--sample-rate", type=int, default=DEFAULT_DECODE_RATE)
    parser.add_argument("--fft-size", type=int, default=DEFAULT_FFT_SIZE)
    parser.add_argument("--hop-size", type=int, default=DEFAULT_HOP_SIZE)
    parser.add_argument("--band-count", type=int, default=DEFAULT_BAND_COUNT)
    parser.add_argument("--no-refine", action="store_true", help="Skip optional local low-frequency raw-waveform refinement.")
    parser.add_argument("--check-dependencies", action="store_true", help="Validate dependencies, then exit without reading media.")
    return parser


def _parse_track_filter(value: str | None) -> set[int] | None:
    if value is None:
        return None
    try:
        numbers = {int(piece.strip()) for piece in value.split(",") if piece.strip()}
    except ValueError as error:
        raise AlignmentError("--tracks must be a comma-separated list of integers.") from error
    if not numbers or any(number <= 0 for number in numbers):
        raise AlignmentError("--tracks must contain positive track numbers.")
    return numbers


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_argument_parser()
    args = parser.parse_args(argv)
    try:
        check_dependencies(args.ffmpeg, args.ffprobe)
        if args.check_dependencies:
            print("Dependencies available: ffmpeg, ffprobe, numpy, pdfplumber")
            return 0
        timelines, pdf_warnings = extract_pdf_timelines(args.pdf)
        tracks, music_warnings = scan_music_tracks(
            args.music_dir,
            args.ffprobe,
            program=args.program,
            release=args.release,
        )
        track_filter = _parse_track_filter(args.tracks)
        if track_filter is not None:
            tracks = [track for track in tracks if track.track_number in track_filter]
            missing = track_filter - {track.track_number for track in tracks}
            if missing:
                raise AlignmentError(f"Requested track files were not found: {sorted(missing)}")
        alignments, video_duration = align_tracks(
            args.video,
            tracks,
            timelines,
            args.ffmpeg,
            args.sample_rate,
            args.fft_size,
            args.hop_size,
            args.band_count,
            refine=not args.no_refine,
        )
        certify_program_alignments(alignments, args.program)
        payload = build_report_payload(
            args,
            timelines,
            alignments,
            video_duration,
            [*pdf_warnings, *music_warnings],
        )
        json_path, markdown_path = write_reports(payload, args.output_dir)
        status_counts: dict[str, int] = {}
        for alignment in alignments:
            status_counts[alignment.status] = status_counts.get(alignment.status, 0) + 1
        summary = ", ".join(f"{key}={value}" for key, value in sorted(status_counts.items()))
        print(f"Alignment complete: {summary}")
        print(f"JSON: {json_path}")
        print(f"Markdown: {markdown_path}")
        needs_review = status_counts.get("unmatched", 0) + status_counts.get("ambiguous", 0)
        return 0 if needs_review == 0 else 2
    except AlignmentError as error:
        parser.exit(1, f"error: {error}\n")


if __name__ == "__main__":
    raise SystemExit(main())
