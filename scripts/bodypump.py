"""Read BODYPUMP's score columns, printed blocks and explicit block repeats."""
from collections import defaultdict
from copy import deepcopy
from fractions import Fraction
from itertools import pairwise
import re


PHRASE = re.compile(r"(?:(\d{1,3})?([¼½¾])?|([0-9]+(?:\.\d+)?))\s*[x×]\s*8", re.I)
BLOCK_REPEAT = re.compile(
    r"^(?:REPEAT|RPT)\s+BLOCKS?\s+(\d{1,2}(?:\s*(?:,|&|\+|AND)\s*\d{1,2})*)(?:\s+(.+))?$", re.I)
NOTE = re.compile(r"^(?:OPTIONS?|RECOVERY|PREVIEW)\s*:|^(?:THEN|AFTER|DOWN AND HOLD|PLATE IN)\b", re.I)
DIRECTION = re.compile(r"(?<![\w/])(?:LEFT|RIGHT|L|R)(?![\w/])", re.I)


def phrase_size(text):
    match = PHRASE.fullmatch(text.strip())
    if not match:
        counts = re.fullmatch(r"(\d{1,3})\s*(?:CTS|COUNTS?)", text.strip(), re.I)
        return Fraction(int(counts[1]), 8) if counts else None
    if not any(match.groups()):
        return None
    if match[3]:
        return Fraction(match[3])
    return Fraction(int(match[1] or 0)) + {
        "¼": Fraction(1, 4), "½": Fraction(1, 2), "¾": Fraction(3, 4)
    }.get(match[2], 0)


def phrase_words(words, top, exercise):
    """Phrase counts are right aligned inside MUSIC, apart from lyrics and REPS."""
    candidates = [word for word in words if exercise - 67 <= word["x0"] < exercise
                  and phrase_size(word["text"]) is not None]
    near_clock = [word for word in candidates if abs(word["top"] - top) <= 4]
    return near_clock or (candidates if len(candidates) == 1 else [])


def block_boundaries(page, words, marks, exercise, *, split_lanes=False):
    """Read vector dividers. A centred printed numeral identifies the block, not its clock."""
    if not marks:
        return {}
    clocks = [word for word in words if any(abs(word["top"] - top) < 0.01 and
              re.fullmatch(r"\d{1,2}[:.][0-5]\d", word["text"]) for top, _ in marks)
              and word["x0"] < exercise]
    if not clocks:
        return {}
    clock_left = min(word["x0"] for word in clocks)
    markers = sorted((word["top"], int(word["text"])) for word in words
                     if word["x1"] <= clock_left - 1 and re.fullmatch(r"[1-9]\d?", word["text"]))
    if not markers:
        return {}
    rules = defaultdict(list)
    for line in page.lines:
        color = line.get("stroking_color")
        if (isinstance(color, tuple) and len(color) == 3
                and (color[0] > color[1] + .25 and color[0] > color[2] + .25)
                and abs(line["bottom"] - line["top"]) <= .5
                and marks[0][0] < line["top"] < marks[-1][0]):
            rules[round(line["top"], 1)].append((line["x0"], line["x1"]))
    dividers = sorted(top for top, ranges in rules.items()
                      if min(a for a, _ in ranges) < clock_left and max(b for _, b in ranges) > exercise
                      and sum(b-a for a, b in ranges) >= page.width * .55)
    if split_lanes:
        dividers = sorted(top for top, spans in rules.items()
                          if any(a <= exercise + 5 and b - a >= page.width * .36 for a, b in spans))
        # A printed recovery separator can use the same rule as a divider.
        # Pair each consecutive block number with its nearest preceding rule.
        dividers = list(dict.fromkeys(max((top for top in dividers if top < y - 3), default=0)
                                      for y, _ in markers))[1:]
    starts = [0, *(i for y in dividers
                   for i in [next((j for j, (top, _) in enumerate(marks) if top > y), len(marks))]
                   if 0 < i < len(marks))]
    starts = list(dict.fromkeys(starts))
    if any(word["text"].upper() == "INTRO" and word["x1"] <= clock_left and
           abs(word["top"] - marks[0][0]) <= 4 for word in words):
        starts = starts[1:]
    result = {}
    for left, right in pairwise([*starts, len(marks)]):
        floor = marks[left][0] - 12
        ceiling = marks[right][0] - 10 if right < len(marks) else page.height * .9
        enclosed = {number for y, number in markers if floor <= y < ceiling}
        if len(enclosed) == 1:
            result[marks[left][1]] = enclosed.pop()
    if list(result.values()) == [number for _, number in markers]:
        return result
    return {}


def split_directive(action):
    lines = action.splitlines()
    note_index = next((index for index, line in enumerate(lines) if index and NOTE.match(line)), len(lines))
    return BLOCK_REPEAT.fullmatch(" ".join(lines[:note_index]).strip()), "\n".join(lines[note_index:])


def mirror(text):
    def replacement(match):
        token = match[0]
        other = {"L": "R", "R": "L", "LEFT": "RIGHT", "RIGHT": "LEFT"}[token.upper()]
        if token.islower():
            return other.lower()
        return other.title() if token.istitle() else other
    return DIRECTION.sub(replacement, text)


def transform_actions(rows, qualifier):
    if not qualifier:
        return True
    text = "\n".join(row["action"] for row in rows)
    stance = re.fullmatch(r"(?:in|with(?:\s+SQUAT)?)\s+(MID|WIDE|WIDER|SET)\s+Stance\.?", qualifier, re.I)
    side = re.fullmatch(r"(?:with\s+(.+?)\s+)?on\s+(?:([LR])(?:\s+side)?|(other side))(?:,\s*([LR])\s+leg\s+B)?\.?",
                        qualifier, re.I)
    if stance and re.search(r"\b(?:MID|WIDE|WIDER|SET)\s+stance\b", text, re.I):
        for row in rows:
            row["action"] = re.sub(r"\b(?:MID|WIDE|WIDER|SET)\s+stance\b",
                                   stance[1].upper() + " Stance", row["action"], flags=re.I)
        return True
    named_setup = re.fullmatch(r"for\s+(.+?)\s+([LR]),\s*([LR])\s+leg\s+B\.?", qualifier, re.I)
    if not side and named_setup:
        actual = re.search(rf"{re.escape(named_setup[1])}\s+{mirror(named_setup[2])}\b", text, re.I)
        if not actual or not re.search(rf"\b{mirror(named_setup[3])}\s+leg\s+B\b", text, re.I):
            return False
        for row in rows:
            row["action"] = mirror(row["action"])
            if row.get("coaching"):
                row["coaching"] = mirror(row["coaching"])
        return True
    if not side:
        return False
    source_side = mirror(side[2].upper()) if side[2] else None
    if side[1]:
        movement = re.escape(side[1])
        proof = re.search(rf"\b{movement}\s+{source_side}\b", text, re.I)
        if not proof:
            return False
        # Transform only the qualifying movement and its setup, not siblings.
        for row in rows:
            if re.search(movement, row["action"], re.I):
                row["action"] = mirror(row["action"])
                if row.get("coaching"):
                    row["coaching"] = mirror(row["coaching"])
        return True
    if not (re.search(rf"(?<![\w/]){source_side}(?![\w/])", text, re.I) if source_side else DIRECTION.search(text)):
        return False
    if side[4] and not re.search(rf"\b{mirror(side[4].upper())}\s+leg\s+B\b", text, re.I):
        return False
    for row in rows:
        row["action"] = mirror(row["action"])
        if row.get("coaching"):
            row["coaching"] = mirror(row["coaching"])
    return True


def expand_blocks(rows, duration, *, phrase_key="counts"):
    """Expand only source-proven block repeats on the recording's printed clock."""
    blocks, current = {}, []
    for row in rows:
        if row.get("block"):
            current = blocks[row["block"]] = []
        current.append(row)
    result, evidence = [], []
    for index, target in enumerate(rows):
        match, note = split_directive(target["action"])
        if not match:
            result.append(target)
            continue
        sources = [int(value) for value in re.findall(r"\d+", match[1])]
        origin = [row for block in sources for row in blocks.get(block, ())]
        if not origin or not all(block in blocks for block in sources) or any(
                "block" in row and row["block"] >= target.get("block", 129)
                or row["time"] >= target["time"] or split_directive(row["action"])[0] for row in origin):
            result.append(target)
            continue
        beats = [phrase_size(row.get(phrase_key, "")) for row in origin]
        size = phrase_size(target.get(phrase_key, ""))
        if (len(beats) > 1 and all(beat is not None for beat in beats)
                and re.fullmatch(r"(?:HOLD|Lower bar to thigh)[.!]?", origin[-1]["action"].strip(), re.I)
                and beats[-1] <= Fraction(1, 2) and sum(beats[:-1]) == size):
            origin, beats = origin[:-1], beats[:-1]
        distance = target["time"] - origin[0]["time"]
        end = rows[index+1]["time"] if index+1 < len(rows) else duration
        if (size is not None and all(beat is not None for beat in beats)
                and len(origin) > 1 and sum(beats[1:]) == size
                and (re.match(r"^(?:Set up|TRANSITION:)", origin[0]["action"], re.I)
                     or (phrase_key != "counts" and re.match(r"^Intro\s*/", origin[0].get("music", ""), re.I)))):
            origin, beats = origin[1:], beats[1:]
            distance = target["time"] - origin[0]["time"]
        appendix = None
        if (size is not None and all(beat is not None for beat in beats) and sum(beats) < size
                and (added := re.match(r"^After last rep, (.+?HOLD)\s*\((\d+)cts\)(?:\n|$)", note, re.I))
                and sum(beats) + Fraction(int(added[2]), 8) == size):
            at = next((j for j, row in enumerate(rows) if row is origin[-1]), None)
            if at is not None and at + 1 < len(rows):
                finish = rows[at + 1]["time"] + distance
                if origin[-1]["time"] + distance < finish < end:
                    appendix = {"time": finish, "page": target["page"], "action": added[1],
                                "counts": added[2] + "cts", "music": "", "repetitions": ""}
                    size -= Fraction(int(added[2]), 8)
                    note = note[added.end():]
        if (any(beat is None for beat in beats) or sum(beats) != size
                or any(a["time"] >= b["time"] for a, b in pairwise(origin))
                or origin[-1]["time"] + distance >= end):
            result.append(target)
            continue
        projected = deepcopy(origin)
        if not transform_actions(projected, match[2]):
            result.append(target)
            continue
        for row in projected:
            row["time"] += distance
            row["page"] = target["page"]
            row.pop("block", None)
            row.pop("repeatsBlock", None)
        # The target's printed lyric is authoritative; internal lyrics were
        # omitted in the score, and are not claimed to have been printed there.
        for row in projected:
            row["music"] = ""
        if appendix:
            projected.append(appendix)
        projected[0]["music"] = target.get("music", "")
        if block := target.get("block"):
            projected[0]["block"] = block
            if len(sources) == 1:
                projected[0]["repeatsBlock"] = sources[0]
        if note:
            cue = projected[-1] if note.upper().startswith(("RECOVERY", "AFTER", "THEN")) else projected[0]
            cue["coaching"] = "\n".join(filter(None, (cue.get("coaching"), note)))
        result.extend(projected)
        evidence.append({"time": target["time"], "block": target.get("block"), "sourceBlocks": sources,
                         "sourceStart": origin[0]["time"], "counts": target["counts"],
                         "times": [row["time"] for row in projected]})
    return result, evidence
