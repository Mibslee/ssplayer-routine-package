"""Match GRIT exercise pages and class orders to independently identified recordings."""
from collections import defaultdict
from pathlib import Path
import re
import unicodedata

from media_analysis import named_grit_formats


COURSE = re.compile(r"GRIT (STRENGTH|CARDIO|ATHLETIC)", re.I)
NUMBERED = re.compile(r"0*([1-9]\d?)[.]\s*(.*)")
PRINTED_TIME = re.compile(r"\b(\d{1,2}):([0-5]\d)\s*mins?\b", re.I)
INTRO_DURATION = re.compile(r"\b(\d{1,3})[-\s]+minute\s+HIIT workout\b|(?<!\d)(\d{1,3})\s*分钟.{0,4}高强度间歇训练", re.I)
SOURCE_NAME = re.compile(
    r"^(?:\d{1,3}[ ._-]+)?((?:[SCA]\d{1,2}(?:-[SCA]\d{1,2})*)|OUTRO)[ ._-]+(.+)$", re.I)
SOURCE_COURSES = {"S": "STRENGTH", "C": "CARDIO", "A": "ATHLETIC"}
DECLARATION = re.compile(r"^(?:DECLARATION OF INTENT|我们的意向声明)$", re.I)
MUSIC_TITLE = re.compile(r"^(.+?)\s+\(\d{1,2}:[0-5]\d\)(?:\s.*)?$")


def normalize(value):
    return unicodedata.normalize("NFKC", value).strip()


def identity_text(value):
    return "".join(character for character in normalize(value).casefold() if character.isalnum())


def normalized_lines(value):
    return [" ".join(line.split()) for raw in (value or "").splitlines() if (line := normalize(raw))]


def toc_entries(lines):
    """Role TOCs have a separate heading and declaration, rather than music credits."""
    if len(lines) < 4 or not (course := COURSE.fullmatch(lines[0])) or lines[1].upper() not in {"MUSIC", "音乐"}:
        return None
    end = next((i for i, line in enumerate(lines[2:], 2) if DECLARATION.fullmatch(line)), None)
    if end is None:
        return None
    entries = {}
    for line in lines[2:end]:
        if match := NUMBERED.fullmatch(line):
            number = int(match[1])
            if number in entries:
                return None
            entries[number] = match[2]
    if len(entries) < 2 or list(entries) != sorted(entries):
        return None
    return course[1].upper(), entries


def technique_heading(lines):
    """Technique pages independently identify the recording paired with its exercise page."""
    header = next((i for i, line in enumerate(lines[:7])
                   if line.upper() in {"TECHNIQUE AND COACHING", "技术与教授"}), None)
    if not header:
        return None
    heading = " ".join(lines[:header])
    numbered = NUMBERED.fullmatch(heading)
    duration = PRINTED_TIME.search(heading)
    if not numbered or not duration:
        return None
    title = heading[numbered.start(2):duration.start()].strip()
    return (int(numbered[1]), title) if title else None


def exercise_heading(page, lines):
    if not lines or not (numbered := NUMBERED.fullmatch(lines[0])) or PRINTED_TIME.search(lines[0]):
        return None
    if not any(line.upper() == "EXERCISE" for line in lines[:45]):
        return None
    number = int(numbered[1])
    words = page.extract_words()
    prefix = next((word for word in words if word["top"] <= page.height * 0.2
                   and word["x0"] <= page.width * 0.2
                   and re.fullmatch(rf"0*{number}[.]", normalize(word["text"]))), None)
    # The Chinese edition puts parts of the title on separate baselines.
    # Read the title band left-to-right between the number and the coaching copy.
    if not prefix:
        return number, numbered[2]
    title = " ".join(word["text"] for word in sorted(words, key=lambda word: word["x0"])
                     if prefix["top"] - 3 <= word["top"] <= prefix["top"] + page.height * 0.035
                     and word["x0"] >= prefix["x1"])
    return number, title or numbered[2]


def outro_tracks(page, lines):
    if not lines or not re.match(r"^GRIT (?:STRENGTH|CARDIO|ATHLETIC)(?:\s|$)", lines[0], re.I):
        return []
    if not any(re.match(r"^OUTRO\b", line, re.I) for line in lines):
        return []
    midpoint = page.width / 2
    texts = [normalized_lines(page.crop((left, 0, right, page.height)).extract_text())
             for left, right in ((0, midpoint), (midpoint, page.width))]
    result = []
    for column in texts:
        for index, line in enumerate(column):
            if not re.match(r"^OUTRO\b", line, re.I):
                continue
            same_row = MUSIC_TITLE.fullmatch(re.sub(r"^OUTRO\s+", "", line, flags=re.I))
            previous = MUSIC_TITLE.fullmatch(column[index - 1]) if index else None
            following = MUSIC_TITLE.fullmatch(column[index + 1]) if index + 1 < len(column) else None
            if match := same_row or previous or following:
                result.append(match[1])
    return result


def index_book(pdf, routine):
    tables, entries, supplemental, official = defaultdict(list), defaultdict(list), defaultdict(list), []
    duration_options = defaultdict(set)
    course = previous = None
    release_seen = False
    for number, page in enumerate(pdf.pages, 1):
        lines = normalized_lines(page.extract_text())
        if not lines:
            previous = None
            continue
        release_seen |= any(re.match(rf"^LES MILLS GRIT\s+{routine}\b", line, re.I) for line in lines)
        if toc := toc_entries(lines):
            course, roles = toc
            tables[course].append((number, roles, lines[1].upper() == "MUSIC"))
            previous = None
            continue
        if len(lines) >= 2 and lines[0].upper() == f"LES MILLS GRIT {routine}" and COURSE.fullmatch(lines[1]):
            course = previous = None
            continue
        official.extend(named_grit_formats("\n".join(lines), number))
        if not course:
            continue
        for title in outro_tracks(page, lines):
            supplemental[identity_text(title)].append(number)
        if technique := technique_heading(lines):
            section, title = technique
            if previous and previous[0] == section and previous[1] == number - 1:
                entries[(course, section)].append((title, previous[1], number, previous[2]))
        previous = None
        if exercise := exercise_heading(page, lines):
            section, role = exercise
            previous = section, number, role
            if section == 1:
                duration = INTRO_DURATION.findall(" ".join(lines[:30]))
                duration_options[course].update(int(first or second) for first, second in duration)
    if not release_seen:
        return {}, {}, {}, [], {}
    return tables, entries, supplemental, official, duration_options


def match_tracks(songs, entries, supplemental):
    by_title = defaultdict(set)
    for key, pages in entries.items():
        for title, _, _, _ in pages:
            by_title[identity_text(title)].add(key)
    assign = defaultdict(list)
    ambiguous = []
    for song in songs:
        if song.get("variant") != "standard":
            continue
        title = song.get("title") or ""
        file = song.get("file")
        source = SOURCE_NAME.fullmatch(Path(file).stem) if file else None
        marker = SOURCE_NAME.fullmatch(title)
        titles = {identity_text(title)}
        if marker:
            titles.add(identity_text(marker[2]))
        if source:
            titles.add(identity_text(source[2]))
        candidates = set().union(*(by_title[value] for value in titles))
        for stated in (source, marker):
            if stated and stated[1].upper() != "OUTRO":
                explicit = {(SOURCE_COURSES[code[0]], int(code[1:])) for code in stated[1].upper().split("-")}
                candidates &= explicit
        if len(candidates) > 1:
            known = song.get("sectionFunction") or {}
            known_page = known.get("page")
            selector = {key for key in candidates if any(known_page in (exercise, technique)
                                                         for _, exercise, technique, _ in entries[key])}
            candidates = selector or candidates
        if len(candidates) == 1:
            assign[candidates.pop()].append(song["section"])
        elif len(candidates) > 1:
            ambiguous.append(song["section"])
        elif (len(matches := titles & supplemental.keys()) == 1
              and all(not stated or stated[1].upper() == "OUTRO" for stated in (source, marker))):
            assign[("OUTRO", matches.pop())].append(song["section"])
    for sections in assign.values():
        if len(sections) > 1:
            ambiguous.extend(sections)
    return {key: sections[0] for key, sections in assign.items() if len(sections) == 1}, sorted(set(ambiguous))


def grit_source_evidence(path, songs, routine):
    """Return proposals only where the numbered source and recording can be reconciled."""
    import pdfplumber

    if not path or not isinstance(routine, str) or not re.fullmatch(r"[1-9]\d{0,3}", routine):
        return None
    with pdfplumber.open(path) as pdf:
        tables, entries, supplemental, official, duration_options = index_book(pdf, routine)
    if not tables or not entries:
        return None
    matched, ambiguous = match_tracks(songs, entries, supplemental)
    tracks = {}
    for key, section in matched.items():
        course, number = key
        if course == "OUTRO":
            sources = sorted(set(supplemental[number]))
            tracks[section] = {"sourceCourse": course, "sourceTrack": "OUTRO",
                               "exercisePages": [], "techniquePages": [], "supplement": True,
                               "pdfPage": sources[0], "openingPages": sources,
                               "roleNames": ["OUTRO"],
                               "sectionFunction": {"name": "OUTRO", "page": sources[0], "approved": True}}
            continue
        pages = entries[key]
        exercises = sorted({exercise for _, exercise, _, _ in pages})
        technique = sorted({technique for _, _, technique, _ in pages})
        roles = [roles[number] for _, roles, english in tables[course] if english and roles.get(number)]
        # A raw Chinese TOC text layer can split a translated caption into unrelated lines.
        names = {identity_text(name): name for name in roles}
        local_roles = [role for _, _, _, role in pages if role]
        if not names and any(number in roles for _, roles, _ in tables[course]):
            names = {identity_text(name): name for name in local_roles}
        canonical = next(iter(names.values())) if len(names) == 1 else None
        record = {"sourceCourse": course, "sourceTrack": f"T{number}",
                  "exercisePages": exercises, "techniquePages": technique, "supplement": False,
                  "pdfPage": exercises[0], "openingPages": exercises + technique,
                  "roleNames": roles + local_roles}
        if canonical:
            record["sectionFunction"] = {"name": canonical, "page": exercises[0], "approved": True}
        tracks[section] = record

    templates, template_pages, template_durations, issues = [], {}, {}, []
    if ambiguous:
        issues.append(f"{'、'.join(ambiguous)} 对应同号或同名的多条 GRIT 录音，需要核对来源课型。")

    def propose(course, references, minutes, sources, seconds=None):
        name = f"GRIT {course.title()} {routine}"
        missing = [f"{source.title()} T{number}" for source, number in references
                   if (source, number) not in matched]
        if missing:
            issues.append(f"{name} · 原谱第 {sources[0]} 页缺少可唯一核对的录音 · {'、'.join(missing)}")
            return
        slots = []
        for identity in references:
            section = matched[identity]
            if not (package := re.fullmatch(r"T([1-9]\d{0,2})[A-Z]?", section)):
                issues.append(f"{name} · 包内小节 {section} 无法映射。")
                return
            slot = {"sectionNumbers": [int(package[1])]}
            if section[-1].isalpha():
                slot["sectionCodes"] = [section]
            if function := tracks[section].get("sectionFunction"):
                slot["name"] = function["name"]
            slots.append(slot)
        key = f"grit-{course.lower()}-{routine}"
        templates.append({"key": key, "name": name, "minutes": minutes, "page": sources[0],
                          "slots": slots, "approved": True})
        template_pages[key] = sources
        if seconds is not None:
            template_durations[key] = seconds

    for course in ("STRENGTH", "CARDIO"):
        if not tables.get(course):
            continue
        sequences = {tuple(roles) for _, roles, _ in tables[course]}
        if len(sequences) != 1:
            issues.append(f"GRIT {course.title()} {routine} · 原谱目录版本的曲目顺序不一致。")
            continue
        numbers = next(iter(sequences))
        minutes = duration_options[course]
        if len(minutes) == 1 and numbers == tuple(range(1, len(numbers) + 1)):
            length = next(iter(minutes))
            if 5 <= length <= 180:
                propose(course, [(course, number) for number in numbers], length,
                        [page for page, _, _ in tables[course]])
    named = defaultdict(list)
    for table in official:
        if any(ref["sourceRelease"] != routine for ref in table["sourceTrackReferences"]):
            issues.append(f"{table['name']} · 原谱第 {table['page']} 页需要另一套路的录音。")
            continue
        named[table["name"]].append(table)
    for name, formats in named.items():
        orders = {(table["sourceTotalSeconds"],
                   tuple((ref["sourceCourse"].removeprefix("GRIT "), ref["sourceTrack"])
                         for ref in table["sourceTrackReferences"])) for table in formats}
        if len(orders) != 1:
            issues.append(f"{name} · 原谱课型表的曲目顺序或总时长互相矛盾。")
            continue
        seconds, order = next(iter(orders))
        if any(not re.fullmatch(r"T[1-9]\d?", track) for _, track in order):
            issues.append(f"{name} · 原谱引用的字母分段需要核对后才能映射。")
            continue
        course = name.removeprefix("GRIT ").upper()
        key = f"grit-{course.lower()}-{routine}"
        if key in template_pages:
            # A named printed format is more specific than the full-length class TOC.
            templates = [template for template in templates if template["key"] != key]
            del template_pages[key]
        propose(course, [(source, int(track[1:])) for source, track in order], formats[0]["minutes"],
                sorted({table["page"] for table in formats}), seconds)
    return {"tracks": tracks, "templates": templates, "templatePages": template_pages,
            "templateDurationSeconds": template_durations, "issues": issues}


def check_grit_section_function(song, evidence):
    function = song.get("sectionFunction")
    record = evidence["tracks"].get(song["section"]) if evidence else None
    if not record or not function:
        return None
    wanted = record.get("sectionFunction")
    if not wanted:
        return None
    accepted_names = {identity_text(name) for name in [wanted["name"], *record["roleNames"]]}
    role_pages = record["openingPages"] if record["supplement"] else record["exercisePages"]
    location = f"{song['section']} 原谱第 {function['page']} 页"
    if function["page"] not in role_pages:
        return "contradiction", f"{location} 与匹配的 GRIT 课型和录音不符。"
    name = normalize(function["name"])
    bilingual = re.fullmatch(r"(.+?)\s*\(([^()]+)\)", name)
    both_names_verified = bilingual and all(identity_text(part) in accepted_names for part in bilingual.groups())
    if identity_text(name) not in accepted_names and not both_names_verified:
        return "contradiction", f"{location} 的小节功能应为「{wanted['name']}」。"
    return "matched", None
