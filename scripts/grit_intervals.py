"""Read GRIT exercise durations without treating them as printed timestamps."""

import math
import re
import unicodedata


TABLE_HEADING = re.compile(r"(?:(DURATION|时长)\s+)?(?:EXERCISES?|动作表|练习表|锻炼表)", re.I)
INTERVAL = re.compile(r"(\d{1,2}):([0-5]\d)\s+(.+)")
FOOTER = re.compile(r"(?:LES MILLS GRIT\b.*|©.*|\d{1,3})", re.I)
PROGRESSION = re.compile(r"(?:Progression|进阶|强度增加)\b", re.I)
NEXT_SET = re.compile(r"(?:Set\s*up\b|准备(?:第|下|巨人组|组|动作))", re.I)
AMRAP = re.compile(r"(?:\bAMRAP\b|最高次数|尽可能多)", re.I)
MAX_END_DIFFERENCE_SECONDS = 2.0
CUE_TOLERANCE_SECONDS = 0.15
MAX_CLOCK_OFFSET_SECONDS = 10.0


def parse_grit_intervals(text, page_number, track_duration, *, anchor_seconds=None):
    """Return proposals for a verified GRIT role page, with no implicit approval.

    An omitted anchor produces a nominal score starting at zero for review.
    Only supply an anchor independently confirmed against the exact recording.
    A verified lead-in can begin before the audio, within the first interval.
    """
    result = {"rows": [], "page": page_number, "pages": [], "approved": False, "status": "notFound",
              "structuralRepeatTimes": [], "warnings": []}
    lines = [" ".join(unicodedata.normalize("NFKC", line).split()) for line in text.splitlines()]
    headers = [(i, match) for i, line in enumerate(lines) if (match := TABLE_HEADING.fullmatch(line))]
    if len(headers) != 1:
        return result
    heading_index, heading = headers[0]
    intervals = []
    unresolved = []
    for line in lines[heading_index + 1:]:
        if not line:
            continue
        if FOOTER.fullmatch(line):
            break
        if match := INTERVAL.fullmatch(line):
            duration = 60 * int(match[1]) + int(match[2])
            if not 0 < duration <= 600:
                unresolved.append(line)
                continue
            intervals.append((duration, match[3], []))
            continue
        if intervals and (PROGRESSION.match(line) or
                          (NEXT_SET.match(line) and re.search(r"\bRECOVERY\b|恢复|休息", intervals[-1][1], re.I))):
            intervals[-1][2].append(line)
        elif intervals and AMRAP.search(intervals[-1][1]):
            # Keep the whole AMRAP at one interval opening. The repeating
            # movements within it do not have individual clock anchors.
            intervals[-1][2].append(line)
        else:
            unresolved.append(line)
    if len(intervals) < 2:
        return result

    durations = [value[0] for value in intervals]
    if (not heading[1] and all(first < second for first, second in zip(durations, durations[1:]))
            and not any(AMRAP.search(action) for _, action, _ in intervals)):
        return {**result, "status": "needsStructureReview",
                "warnings": ["表格数字严格递增，需确认是段时长还是曲内时间点。"]}

    start = 0 if anchor_seconds is None else anchor_seconds
    if (not math.isfinite(start) or not math.isfinite(track_duration) or track_duration <= 0
            or not -durations[0] < start < track_duration):
        raise ValueError("GRIT interval anchor must place the opening interval within the recording.")
    now = start
    rows = []
    for duration, action, notes in intervals:
        cue = {"time": round(max(0, now), 4), "action": action, "page": page_number}
        if notes:
            cue["coaching"] = "\n".join(notes)
        rows.append(cue)
        now += duration
    difference = round(track_duration - now, 4)
    result.update(page=page_number, pages=[page_number], totalSeconds=sum(durations), trackDuration=track_duration,
                  anchorSeconds=anchor_seconds, endDifferenceSeconds=difference, unresolvedRows=unresolved,
                  rows=rows, status="review")

    warnings = result["warnings"]
    if anchor_seconds is None:
        warnings.append("曲内位置按原谱逐段累加预览。需在对应录音或视频核实第一段起点。")
    elif start < 0:
        result["clippedOpeningSeconds"] = round(-start, 4)
    if abs(difference) > MAX_END_DIFFERENCE_SECONDS:
        warnings.append(f"原谱累计结束与录音结尾相差 {abs(difference):.1f} 秒，需确认尾段或版本。")
    if any(AMRAP.search(row["action"]) for row in rows):
        warnings.append("AMRAP 保留为一个区间，组内动作只保留文字，需核对结构。")
    if unresolved:
        warnings.append("表内有未定位时间的文字行，需核对它们属于哪个区间。")
    if any(row["time"] >= track_duration for row in rows):
        result.update(rows=[], status="contradiction")
        warnings.append("累加得到的动作起点超出了录音范围。")
    elif unresolved or any(AMRAP.search(row["action"]) for row in rows):
        result["status"] = "needsStructureReview"
    elif anchor_seconds is None:
        result["status"] = "needsAnchor"
    elif abs(difference) > MAX_END_DIFFERENCE_SECONDS:
        result["status"] = "needsDurationReview"
    return result


def interval_candidates(pdf_path, page_number, track_duration, *, anchor_seconds=None):
    import pdfplumber

    with pdfplumber.open(pdf_path) as pdf:
        if not 1 <= page_number <= len(pdf.pages):
            raise ValueError(f"The original PDF has no physical page {page_number}.")
        text = pdf.pages[page_number - 1].extract_text(layout=True)
    return parse_grit_intervals(text or "", page_number, track_duration, anchor_seconds=anchor_seconds)


def normalized_interval_text(value):
    return " ".join(unicodedata.normalize("NFKC", value or "").casefold().split())


def validate_interval_review(proposal, song_cues, *, observed_anchor_seconds=None):
    """Recheck every packaged cue against a GRIT source table.

    With no independently observed anchor this verifies relative placement
    only. The second cue establishes a common signed offset because the
    first cue may be clipped to zero.
    """
    source = proposal["rows"]
    if not source or proposal.get("unresolvedRows"):
        return "manual", "原谱区间尚未可靠解析，无法核对动作或时间点。"
    if len(song_cues) != len(source):
        return "contradiction", f"原谱有 {len(source)} 个区间，包内有 {len(song_cues)} 个动作时间点。"

    source_anchor = proposal["anchorSeconds"] or 0
    first_duration = source[1]["time"] - source_anchor
    anchor = song_cues[1]["time"] - first_duration
    if abs(anchor) > MAX_CLOCK_OFFSET_SECONDS or anchor <= -first_duration:
        return "contradiction", f"动作时间线的共同偏移 {anchor:.2f} 秒超出可复核范围。"
    if (observed_anchor_seconds is not None
            and abs(anchor - observed_anchor_seconds) > CUE_TOLERANCE_SECONDS):
        return "contradiction", "动作时间线的共同偏移与已核实的曲内起点不符。"

    for index, (expected, actual) in enumerate(zip(source, song_cues), 1):
        if (actual["page"] != expected["page"]
                or normalized_interval_text(actual["action"]) != normalized_interval_text(expected["action"])):
            return "contradiction", f"第 {index} 个区间的动作或来源页与原谱不符。"
        if expected.get("coaching") and normalized_interval_text(expected["coaching"]) not in normalized_interval_text(actual.get("coaching")):
            return "contradiction", f"第 {index} 个区间漏了原谱的组内动作或进阶说明。"
        relative = expected["time"] - source_anchor if index > 1 else 0
        expected_time = max(0, relative + anchor)
        if (not 0 <= actual["time"] < proposal["trackDuration"]
                or abs(actual["time"] - expected_time) > CUE_TOLERANCE_SECONDS):
            return "contradiction", f"第 {index} 个区间在曲内应为 {expected_time:.2f} 秒，包内为 {actual['time']:.2f} 秒。"
    return "matched", None
