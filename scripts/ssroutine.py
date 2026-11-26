#!/usr/bin/env python3
"""Author and verify a single unencrypted SSplayer routine package."""

from __future__ import annotations

import argparse
from datetime import date
import hashlib
import json
import math
import os
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import unicodedata
from io import BytesIO
from pathlib import Path
from time import perf_counter

from grit_evidence import check_grit_section_function, grit_source_evidence
from grit_intervals import parse_grit_intervals, validate_interval_review
from media_analysis import (alignment_module, audio_packet_hash, balance_audio, efficient_video, extract_cues,
                            class_format_suggestions, inspect_section_functions, locate_chapters, map_recordings, measure, optimize_video, probe, run, timing_failure_detail, timing_windows,
                            unresolved_repeat_times, video_info)


MAGIC = b"SSRT0001"
MIB = 1024 * 1024
MAX_MANIFEST = 4 * MIB
MAX_PACKAGE = 12 * 1024 * MIB
MAX_PDF_MIB = 64
AUDIO = {"aac", "aif", "aiff", "flac", "m4a", "mp3", "wav"}
VIDEO = {"mp4", "m4v", "mov"}
PICTURE = {"jpg", "jpeg", "png"}
VARIANTS = {"standard", "bonus", "alternative", "combo45", "combo45Alternative", "combo45Bonus", "outro", "preClass"}
PROGRAM_ALBUMS = {
    "BODYCOMBAT": "BC", "BODYJAM": "BJ", "BODYPUMP": "BP", "BODYATTACK": "BA", "RPM": "RPM",
    "BODYBALANCE": "BB", "BODYFLOW": "BB", "BODYSTEP": "BS",
    "BC": "BC", "BJ": "BJ", "BP": "BP", "BA": "BA", "BB": "BB", "BS": "BS", "CORE": "CORE", "TONE": "TONE", "GRIT": "GRIT",
}
PROGRAM_ALIASES = {**PROGRAM_ALBUMS, **{f"LESMILLS{name}": code for name, code in PROGRAM_ALBUMS.items()},
                   "莱美核心": "CORE", "莱美核心训练": "CORE", "莱美GRIT": "GRIT", "莱美TONE": "TONE",
                   "莱美有氧": "BA", "莱美有氧训练": "BA"}
NATIVE_PROGRAMS = {"BC", "BJ", "BP", "RPM"}
VIDEO_PROFILES = {"efficient": (None, None), "ipad": (1080, 1800), "compact": (720, 950)}
DEFAULT_LOUDNESS_TARGET = -16.0


def require(condition, reason):
    if not condition:
        raise ValueError(reason)


def extension(path):
    value = path.suffix.lower().lstrip(".")
    return "jpg" if value == "jpeg" else value


def digest(path):
    with path.open("rb") as source:
        with_hash = hashlib.sha256()
        while chunk := source.read(MIB):
            with_hash.update(chunk)
    return with_hash.hexdigest()


def identity(path):
    require(path.is_file(), f"Not a regular file: {path}")
    before = path.stat()
    value = {"ext": extension(path), "bytes": before.st_size, "sha256": digest(path)}
    after = path.stat()
    require((before.st_ino, before.st_size, before.st_mtime_ns) ==
            (after.st_ino, after.st_size, after.st_mtime_ns), f"File changed during analysis: {path.name}")
    return value


def checked_pdf_path(value):
    path = Path(value).resolve(strict=True)
    require(path.is_file() and extension(path) == "pdf",
            f"请选择可读取的原谱 PDF：{path.name}")
    size = path.stat().st_size
    require(size > 0, f"原谱 PDF 是空文件：{path.name}")
    require(size <= MAX_PDF_MIB * MIB,
            f"原谱 PDF 超过 {MAX_PDF_MIB} MiB：{path.name}。请使用保留页序和文字的压缩副本。")
    return path


def write_json(path, data):
    with path.open("x", encoding="utf-8") as output:
        json.dump(data, output, ensure_ascii=False, allow_nan=False, indent=2)
        output.write("\n")


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def read_identity(value):
    program, routine, revision = value["program"], value["routine"], value["revision"]
    require(re.fullmatch(r"[A-Z][A-Z0-9]{1,15}", program) is not None, "Invalid program code")
    require(re.fullmatch(r"[A-Z0-9][A-Z0-9-]{0,15}", routine) is not None
            and (not routine.isdigit() or int(routine) > 0), "Invalid routine number")
    require(type(revision) is int and 0 < revision <= 2147483647, "Revision must be a positive integer")
    if (text := value.get("releaseDate")) is not None:
        match = re.fullmatch(r"([0-9]{4})(?:Q[1-4]|-([0-9]{2})(?:-([0-9]{2}))?)?", text)
        require(match, "Use YYYY, YYYYQn, YYYY-MM or YYYY-MM-DD for the release date")
        date(int(match[1]), int(match[2] or 1), int(match[3] or 1))
    return program, routine, revision


def author_identity(project, routine):
    raw = project.strip().upper()
    key = re.sub(r"[\s._-]", "", raw)
    program = PROGRAM_ALIASES.get(key, raw)
    number = routine.strip().upper()
    for prefix in (key, program):
        remainder = number.removeprefix(prefix).lstrip(" -_")
        if re.fullmatch(r"[0-9]+", remainder) and int(remainder) > 0:
            return program, str(int(remainder))
    return program, number


def labeled_album(path, program, routine):
    duration, media = probe(path, "audio")
    tags = {key.lower(): value for key, value in media["format"].get("tags", {}).items()}
    album = re.sub("[^A-Z0-9]", "", tags.get("album", "").upper())
    names = "|".join(PROGRAM_ALBUMS)
    label = re.match(rf"^(?:LESMILLS)?({names})([1-9][0-9]{{0,2}})(?![0-9])", album)
    if program in PROGRAM_ALBUMS.values() and routine.isdigit() and label:
        source = PROGRAM_ALBUMS[label[1]], int(label[2])
        if source != (program, int(routine)):
            return duration, f"The album tag points to {source[0]} {source[1]}.", []
    attached = [stream["index"] for stream in media["streams"]
                if stream["codec_type"] == "video" and stream.get("disposition", {}).get("attached_pic")]
    return duration, None, attached


def track_stem(stem, program, routine):
    aliases = {program} | {name for name, code in PROGRAM_ALIASES.items() if code == program}
    names = "|".join(re.escape(name) for name in sorted(aliases, key=lambda name: (-len(name), name)))
    release = ("0*" if routine.isdigit() else "") + re.escape(routine)
    match = re.match(rf"^(?:{names})[ ._-]*{release}[ ._-]+(.+)$", stem, re.I)
    return match[1] if match else stem


def cover_extension(source, names=None):
    from PIL import Image

    try:
        with Image.open(source) as image:
            ext = {"JPEG": "jpg", "PNG": "png"}.get(image.format)
            require(ext and all(1 <= size <= 8192 for size in image.size),
                    "Choose a JPEG or PNG image up to 8192 pixels per side.")
            image.load()
            if names:
                from text_cover import NAME_KEYS

                artwork_names = tuple(image.info.get(key) for key in NAME_KEYS)
                require(not any(artwork_names) or artwork_names == names,
                        "课程或套路名称已变更，请重新生成并确认文字封面。")
            return ext
    except (OSError, Image.DecompressionBombError) as error:
        raise ValueError("The cover image cannot be decoded.") from error


def cover_asset(path, names=None):
    require(path.is_file(), "Choose a local cover image.")
    with path.open("rb") as artwork:
        data = artwork.read(8 * MIB + 1)
    require(0 < len(data) <= 8 * MIB, "Choose a cover image of at most 8 MiB.")
    ext = cover_extension(BytesIO(data), names)
    require(extension(path) == ext, "The cover extension must match the image format.")
    return {"ext": ext, "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}


def list_cover_candidates(files, songs, embedded, plan_path):
    candidates = {}
    for path in files:
        try:
            asset = cover_asset(path)
        except (ValueError, OSError):
            continue
        candidates.setdefault(asset["sha256"], {"file": str(path), "source": "image"})
    for song in songs:
        for index in embedded.get(song["file"], ()):
            try:
                data = run("ffmpeg", "-nostdin", "-v", "error", "-i", song["file"], "-map", f"0:{index}",
                           "-c:v", "copy", "-frames:v", "1", "-f", "image2pipe", "-")
                require(0 < len(data) <= 8 * MIB, "Embedded artwork is too large.")
                sha = hashlib.sha256(data).hexdigest()
                if sha in candidates:
                    continue
                ext = cover_extension(BytesIO(data))
            except (ValueError, OSError):
                continue
            previews = plan_path.resolve().with_name(f".{plan_path.stem}-covers")
            previews.mkdir(parents=True, exist_ok=True)
            path = previews / f"{sha}.{ext}"
            if path.exists():
                require(digest(path) == sha, "A saved cover preview was modified. Choose a different plan filename.")
            else:
                with path.open("xb") as output:
                    output.write(data)
            candidates[sha] = {"file": str(path), "source": "embedded", "section": song["section"]}
    return list(candidates.values())


def set_cover_selection(config, choice):
    if choice is None:
        config.pop("cover", None)
        config["coverSelection"] = {"omit": True}
        return None
    path = choice.resolve(strict=True)
    asset = cover_asset(path, (config.get("projectName"), config.get("routineName")))
    config["cover"] = str(path)
    config["coverSelection"] = {"sha256": asset["sha256"]}
    return asset


def selected_cover(config):
    selected = config.get("coverSelection")
    if path := config.get("cover"):
        require(selected and selected.get("sha256"),
                "Show the artwork to the creator and run select-cover with the confirmed image.")
        asset = cover_asset(Path(path), (config.get("projectName"), config.get("routineName")))
        require(selected == {"sha256": asset["sha256"]},
                "The confirmed artwork changed. Show it again and run select-cover.")
        return asset
    if config.get("coverCandidates") or selected:
        require(selected == {"omit": True},
                "Show the cover candidates to the creator. Run select-cover with the approved image or --without-cover.")
    return None


def select_cover(args):
    document = read_json(args.input)
    config = document.get("plan", document)
    read_identity(config)
    confirmed = config.get("identityConfirmation") is not None
    if confirmed:
        validate_confirmation(config)
    asset = set_cover_selection(config, args.cover)
    if confirmed:
        config["identityConfirmation"] = confirmation_values(config)
    if config is not document:
        if asset:
            document["cover"] = asset
        else:
            document.pop("cover", None)
    write_json(args.output, document)


def attribution_path():
    if directory := os.environ.get("SSPLAYER_CREATOR_CONFIG_DIR"):
        return Path(directory).expanduser() / "attribution.json"
    directory = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return directory / "ssplayer_package_creator" / "attribution.json"


def default_credit():
    try:
        credit = read_json(attribution_path())["credit"]
        return credit if valid_text(credit, 128, single_line=True) and credit.strip() == credit else None
    except (OSError, KeyError, TypeError, ValueError):
        return None


def save_default_credit(credit):
    path = attribution_path()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as output:
        temporary = Path(output.name)
        try:
            json.dump({"credit": credit}, output, ensure_ascii=False)
            output.write("\n")
            output.close()
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)


def attribution(args):
    if args.reset:
        attribution_path().unlink(missing_ok=True)
    credit = default_credit()
    state = "name" if credit else "none" if credit == "" else "unset"
    print(json.dumps({"state": state, "credit": credit or None}, ensure_ascii=False))


def public_credit(value):
    name = value.strip()
    if not name or not valid_text(name, 128, single_line=True):
        raise argparse.ArgumentTypeError("Enter the public name, or use --no-credit for no attribution.")
    return name


def confirm_plan(args):
    config = read_json(args.input)
    config["projectName"] = args.project_name.strip()
    config["routineName"] = args.routine_name.strip()
    if args.credit is not None:
        config["credit"] = args.credit.strip() or None
    elif "credit" not in config and (credit := default_credit()) is not None:
        config["credit"] = credit or None
    require("credit" in config, "Enter the public name with --credit, or explicitly select --no-credit.")
    require(not args.remember_credit or args.credit is not None,
            "To save a new default, enter the public name with --credit or explicitly select --no-credit.")
    if args.cover or args.without_cover:
        set_cover_selection(config, args.cover)
    require(config.get("coverSelection"), "Confirm the artwork or use --without-cover.")
    selected_cover(config)
    config["identityConfirmation"] = confirmation_values(config)
    write_json(args.output, config)
    if args.remember_credit:
        save_default_credit(config["credit"] or "")


def text_cover(args):
    from text_cover import render

    config = read_json(args.input)
    program, _, _ = read_identity(config)
    for argument, key in (("project_name", "projectName"), ("routine_name", "routineName")):
        if getattr(args, argument) is not None:
            config[key] = getattr(args, argument).strip()
    validate_display_metadata(config)
    require(config.get("projectName") and config.get("routineName"), "请填写课程名称和套路名称。")
    render(args.output, program=program, project=config["projectName"], routine=config["routineName"], font_path=args.font)


def canonical_duplicate(items):
    paths = [Path(item["file"]) for item, _ in items]
    durations = [duration for _, duration in items]
    if len({extension(path) for path in paths}) != 1 or max(durations) - min(durations) > 0.025:
        return None
    try:
        if len({audio_packet_hash(path) for path in paths}) == 1:
            return min((item for item, _ in items), key=lambda item: (len(Path(item["file"]).stem), item["file"]))
    except (ValueError, OSError):
        return None
    return None


def plan(args):
    root = args.source.resolve(strict=True)
    require(root.is_dir(), "Select a source folder")
    require(not args.output.exists(), "Choose a new output path for the plan.")
    program, routine = author_identity(args.program, args.routine)
    value = {"program": program, "routine": routine, "revision": args.revision, "music": [],
             "projectName": args.project_name or args.program.strip(),
             "routineName": args.routine_name or f"{program} {routine}",
             "identityConfirmation": None}
    credit = args.credit if args.credit is not None else default_credit()
    if credit is not None:
        value["credit"] = credit.strip() or None
    if args.release_date:
        value["releaseDate"] = args.release_date
    note_fields = (("video", "video_note"), ("timeline", "timeline_note"))
    notes = {name: note.strip() for name, field in note_fields if (note := getattr(args, field, None))}
    if notes:
        validate_quality_notes(notes)
        value["qualityNotes"] = notes
    read_identity(value)
    recognized = {}
    assets = {"video": [], "pdf": []}
    cover_images, embedded = [], {}
    review = []
    warnings = []
    for file in sorted(root.rglob("*")):
        if not file.is_file() or file.is_symlink() or any(part.startswith(".") for part in file.relative_to(root).parts):
            continue
        suffix = extension(file)
        if suffix in AUDIO:
            stem = track_stem(file.stem, program, routine)
            match = re.match(r"^(?:(ALTERNATIVE|ALT|EXPRESS|BNS|BONUS|替换曲|替换|备选|A|B|E)[ ._-]*)?(?:T[ ._-]*)?(\d{1,3})([A-Z]?)(?:[ ._-]+(.*)|$)",
                             stem, re.I)
            if not match or not 1 <= int(match[2]) <= 128:
                review.append(str(file))
                continue
            prefix, number, letter, title = match.groups()
            markers = {"ALT": "alternative", "ALTERNATIVE": "alternative", "A": "alternative",
                       "替换曲": "alternative", "替换": "alternative", "备选": "alternative",
                       "EXPRESS": "alternative", "E": "alternative",
                       "BNS": "bonus", "B": "bonus", "BONUS": "bonus"}
            variant = markers.get((prefix or "").upper(), "standard")
            suffix = re.match(r"^(ALTERNATIVE|ALT|BONUS|BNS|替换曲|替换|备选)(?:[ ._-]+(.*)|$)",
                              title or "", re.I)
            if suffix:
                explicit = markers[suffix[1].upper()]
                if prefix and explicit != variant:
                    review.append(str(file))
                    warnings.append({"file": file.name, "reason": "Conflicting variant labels."})
                    continue
                variant, title = explicit, suffix[2]
            section = f"T{int(number)}{letter.upper()}"
            if value["program"] in NATIVE_PROGRAMS - {"BJ"} and letter:
                review.append(str(file))
                warnings.append({"file": file.name, "section": section,
                                 "reason": "This native catalogue requires a whole section. Check the source."})
                continue
            try:
                duration, conflict, attached = labeled_album(file, value["program"], value["routine"])
            except (ValueError, TypeError, KeyError):
                duration, conflict, attached = 0, "Audio tracks or duration could not be verified.", []
            if conflict:
                review.append(str(file))
                warnings.append({"file": file.name, "section": section, "reason": conflict})
                continue
            item = {"section": section, "variant": variant, "file": str(file), "title": title or section}
            recognized.setdefault((section, variant), []).append((item, duration))
            if attached:
                embedded[str(file)] = attached
        elif suffix in VIDEO:
            assets["video"].append(str(file))
        elif suffix == "pdf":
            assets["pdf"].append(str(file))
        elif suffix in PICTURE:
            cover_images.append(file)
    for items in recognized.values():
        if len(items) == 1:
            value["music"].append(items[0][0])
        elif choice := canonical_duplicate(items):
            value["music"].append(choice)
            warnings.append({"file": Path(choice["file"]).name, "section": choice["section"],
                             "reason": f"Kept one of {len(items)} copies with identical encoded audio."})
        else:
            review.extend(item["file"] for item, _ in items)
    def song_key(row):
        section = re.fullmatch(r"T(\d+)([A-Z]?)", row["section"])
        return (row["variant"] != "standard", int(section[1]), section[2], row["variant"])
    value["music"].sort(key=song_key)
    for name, paths in assets.items():
        if len(paths) == 1:
            value[name] = paths[0]
        elif paths:
            review.extend(paths)
    if args.cover or args.without_cover:
        set_cover_selection(value, args.cover)
    else:
        value["coverCandidates"] = list_cover_candidates(cover_images, value["music"], embedded, args.output)
    if args.project_name and args.routine_name and args.credit is not None and value.get("coverSelection"):
        value["identityConfirmation"] = confirmation_values(value)
    value["reviewSources"] = review
    value["musicWarnings"] = warnings
    write_json(args.output, value)
    print(json.dumps({"musicProposed": len(value["music"]), "reviewSources": len(review),
                      "musicWarnings": len(warnings),
                      "coverCandidates": len(value.get("coverCandidates", [])),
                      "projectName": value["projectName"], "routineName": value["routineName"],
                      "credit": value.get("credit"), "hasCreditChoice": "credit" in value}, ensure_ascii=False))


def grit_interval_proposals(pdf, songs, evidence):
    if not evidence:
        return {}
    import pdfplumber

    results = {}
    with pdfplumber.open(pdf) as document:
        for song in songs:
            record = evidence["tracks"].get(song["section"], {})
            if song["variant"] != "standard":
                continue
            for page in reversed(record.get("exercisePages", [])):
                text = document.pages[page - 1].extract_text(layout=True) or ""
                suggestion = parse_grit_intervals(text, page, song["duration"])
                if suggestion["status"] != "notFound":
                    results[song["section"]] = suggestion
                    break
    return results


def analyze(args):
    started = perf_counter()
    config = read_json(args.plan)
    program, _, _ = read_identity(config)
    validate_confirmation(config)
    cover = selected_cover(config)
    pdf = checked_pdf_path(config["pdf"]) if config.get("pdf") else None
    require(not args.output.exists(), "Choose a new review path. Keep the previous review for comparison.")
    songs = config["music"]
    require(1 <= len(songs) <= 128, "Choose the music files in the plan")
    validate_slots(program, songs)
    def inspect_audio(row):
        path = Path(row["file"]).resolve(strict=True)
        require(extension(path) in AUDIO, f"Unsupported audio: {path.name}")
        duration, _ = probe(path, "audio")
        return {**row, "file": path, "duration": duration, "source": identity(path)}

    audio = map_recordings(inspect_audio, songs)
    review = {"plan": config, "programRoutine": [config["program"], config["routine"]],
              "tracks": [{"source": row["source"], "duration": row["duration"], "selection": source_selection(row)}
                         for row in audio]}
    if pdf:
        page_count, cues, warnings = extract_cues(pdf, audio, ocr=args.ocr)
        review.update(pdf={"source": identity(pdf), "pageCount": page_count}, pdfWarnings=warnings)
        review["classTemplates"] = class_format_suggestions(pdf)
        grit = grit_source_evidence(pdf, audio, config["routine"]) if program == "GRIT" else None
        intervals = grit_interval_proposals(pdf, audio, grit)
        if grit:
            resolved_pages = {page for pages in grit["templatePages"].values() for page in pages}
            review["classTemplates"] = [template for template in review["classTemplates"]
                                        if not (template.get("sourceTrackReferences")
                                                and resolved_pages.intersection(template["sourcePages"]))]
            review["classTemplates"].extend(grit["templates"])
            review["sourceStructureWarnings"] = grit["issues"]
        for song, record, rows in zip(songs, review["tracks"], cues):
            if function := rows.pop("sectionFunction", None):
                record["sectionFunction"] = function
            record["cues"] = rows
            if grit and song["variant"] == "standard" and (source := grit["tracks"].get(song["section"])):
                record.update(pdfOpeningPage=source["pdfPage"], supplementary=source["supplement"])
                if function := source.get("sectionFunction"):
                    record["sectionFunction"] = function
                if intervals_for_track := intervals.get(song["section"]):
                    record["intervalReview"] = intervals_for_track
            stem = track_stem(Path(song["file"]).stem, program, config["routine"])
            if song["variant"] == "alternative" and re.match(r"(?i)^(?:EXPRESS|E)[ ._-]*\d+", stem):
                number = int(re.search(r"\d+", song["section"])[0])
                editions = {(slot["requiredDurationMinutes"], template["page"])
                            for template in review["classTemplates"] for slot in template["slots"]
                            if slot.get("requiredDurationMinutes") and number in slot["sectionNumbers"]}
                if len(editions) == 1:
                    minutes, page = next(iter(editions))
                    record["classEdition"] = {"minutes": minutes, "page": page, "approved": False}
    if config.get("video"):
        video = Path(config["video"]).resolve(strict=True)
        duration, metadata = probe(video, "video")
        review["video"] = {"source": identity(video), "duration": duration}
        video_audio = next((s for s in metadata["streams"] if s["codec_type"] == "audio"), None)
        if video_audio and not args.skip_alignment:
            print("正在核对各小节的音画定位", file=sys.stderr, flush=True)
            try:
                audio_duration = float(video_audio.get("duration", duration))
            except (TypeError, ValueError):
                audio_duration = duration
            chapters, warnings = locate_chapters(video, audio, duration, audio_duration)
            review["videoWarnings"] = warnings
            for record, chapter in zip(review["tracks"], chapters):
                record["chapter"] = chapter
    if cover:
        review["cover"] = cover
    write_json(args.output, review)
    print(json.dumps({"music": len(audio),
                      "audioMatched": sum(bool((x.get("chapter") or {}).get("approved")) for x in review["tracks"]),
                      "chaptersProposed": sum(bool(x.get("chapter")) for x in review["tracks"]),
                      "cueRowsProposed": sum(len(x.get("cues", {}).get("rows", [])) for x in review["tracks"]),
                      "intervalCueRowsToReview": sum(len(x.get("intervalReview", {}).get("rows", []))
                                                      for x in review["tracks"]),
                      "sectionFunctionsProposed": sum("sectionFunction" in record for record in review["tracks"]),
                      "classTemplatesProposed": sum(not item.get("sourceReview") for item in review.get("classTemplates", [])),
                      "classTemplateManualReview": [item["name"] for item in review.get("classTemplates", [])
                                                    if item.get("sourceReview")],
                      "seconds": round(perf_counter() - started, 2)}))


def same_source(value, expected):
    path = Path(value).resolve(strict=True)
    require(identity(path) == expected, f"Source changed since review: {path.name}")
    return path


def source_selection(song):
    return {key: song[key] for key in ("section", "variant", "catalogTrackID", "pdfPages") if key in song}


def approved_timeline(song, findings):
    analysis = findings.get("cues")
    if not analysis or analysis.get("approved") is not True:
        return None
    label = f"{song['section']} ({song['variant']})"
    rows = analysis["rows"]
    pages = analysis["pages"]
    require(rows and pages and all(row["page"] in pages for row in rows),
            f"{label}: verify the complete timed rows against the selected PDF pages")
    repeats = unresolved_repeat_times(rows)
    require(not repeats, f"{label}: expand the structural repeat at {', '.join(map(str, repeats))} seconds before packing")
    clocks = {row["time"] for row in rows}
    missing = [start for start in analysis.get("structuralRepeatTimes", []) if start not in clocks]
    require(not missing, f"{label}: preserve the printed repeat opening at {', '.join(map(str, missing))} seconds")
    return rows


def valid_text(value, limit, *, single_line=False):
    return (isinstance(value, str) and len(value.encode("utf-8")) <= limit
            and all(unicodedata.category(character) not in {"Cc", "Cf"}
                    or (not single_line and character in "\n\t") for character in value))


def validate_display_metadata(value):
    for key in ("projectName", "routineName"):
        if (name := value.get(key)) is not None:
            require(valid_text(name, 240, single_line=True) and 0 < len(name) <= 68
                    and name.strip() == name and not any(unicodedata.category(character) in {"Zl", "Zp"} for character in name),
                    f"Invalid {key}. Use a single line of up to 68 characters and 240 UTF-8 bytes.")
    if (credit := value.get("credit")) is not None:
        require(valid_text(credit, 128, single_line=True) and credit, "Invalid contributor credit")


def confirmation_values(value):
    read_identity(value)
    validate_display_metadata(value)
    require(value.get("projectName") and value.get("routineName"), "Confirm both display names.")
    fields = ("program", "routine", "revision", "projectName", "routineName", "credit", "releaseDate", "coverSelection")
    return {key: value.get(key) for key in fields}


def validate_confirmation(value):
    if "identityConfirmation" in value:
        require(value["identityConfirmation"] is not None and value["identityConfirmation"] == confirmation_values(value),
                "Confirm project and routine names and the credit choice with confirm-plan before analysis or build.")
    else:
        validate_display_metadata(value)  # Existing review files remain usable.


def validate_section_function(function, pdf, section):
    require(isinstance(function, dict) and set(function) == {"name", "page"},
            f"Invalid section function for {section}")
    name, page = function["name"], function["page"]
    require(valid_text(name, 240, single_line=True) and 0 < len(name) <= 68 and name.strip() == name
            and not any(unicodedata.category(character) in {"Zl", "Zp"} for character in name),
            f"Use the printed single-line section function for {section}.")
    require(pdf and type(page) is int and 1 <= page <= pdf["pageCount"],
            f"Section function for {section} requires a physical page in the source PDF.")


def approved_section_function(song, findings, pdf):
    function = findings.get("sectionFunction")
    if not isinstance(function, dict) or function.get("approved") is not True:
        return None
    value = {key: function[key] for key in ("name", "page") if key in function}
    validate_section_function(value, pdf, song["section"])
    return value


def validate_slots(program, songs):
    program = PROGRAM_ALIASES.get(program, program)
    slots = set()
    recordings = set()
    for song in songs:
        section = re.fullmatch(r"T([1-9][0-9]{0,2})([A-Z]?)", song["section"])
        require(section and int(section[1]) <= 128 and song["variant"] in VARIANTS,
                f"Invalid music section or variant: {song['section']}")
        require(not section[2] or program == "BJ" or program not in NATIVE_PROGRAMS,
                f"{program} requires a whole section: {song['section']}")
        for key, limit in (("catalogTrackID", 128), ("title", 256)):
            if (value := song.get(key)) is not None:
                require(value and valid_text(value, limit), f"Invalid {key} in {song['section']}")
        key = (song["section"], song["variant"], song.get("catalogTrackID") if program in NATIVE_PROGRAMS else None)
        require(key not in slots, f"Duplicate music slot: {key[0]} · {key[1]}")
        slots.add(key)
        if program not in NATIVE_PROGRAMS and "asset" in song:
            sha = song["asset"]["sha256"]
            require(sha not in recordings, f"The same audio bytes occupy two slots: {song['section']}")
            recordings.add(sha)


def validate_quality_notes(notes):
    require(isinstance(notes, dict) and set(notes) <= {"video", "timeline"}
            and all(isinstance(text, str) and text.strip() and valid_text(text, 256, single_line=True)
                    for text in notes.values()), "Invalid quality note")


def validate_class_templates(templates, pdf):
    require(isinstance(templates, list) and len(templates) <= 12, "The pack can contain up to 12 class templates.")
    require(not templates or pdf, "Class templates require the original course PDF.")
    keys = set()
    for template in templates:
        key, name, minutes, page, slots = (template[k] for k in ("key", "name", "minutes", "page", "slots"))
        require(isinstance(key, str) and re.fullmatch(r"[a-z0-9][a-z0-9-]{0,39}", key) and key not in keys
                and valid_text(name, 240, single_line=True) and name and name.strip() == name and len(name) <= 68
                and type(minutes) is int and 5 <= minutes <= 180 and type(page) is int and 1 <= page <= pdf["pageCount"]
                and isinstance(slots, list) and 1 <= len(slots) <= 64, "Invalid class template identity, duration, or source page.")
        keys.add(key)
        for slot in slots:
            numbers = slot["sectionNumbers"]
            edition = slot.get("requiredDurationMinutes")
            mode = slot.get("selectionMode", "single")
            require(isinstance(numbers, list) and 1 <= len(numbers) <= 20
                    and all(type(x) is int and 1 <= x <= 128 for x in numbers) and len(set(numbers)) == len(numbers)
                    and slot.get("variantPreference", "any") in ("standard", "alternative", "any")
                    and mode in ("single", "consecutive")
                    and type(slot.get("enforcesVariant", False)) is bool
                    and type(slot.get("isOptional", False)) is bool
                    and (edition is None or (type(edition) is int and 5 <= edition <= 180))
                    and valid_text(slot.get("name", ""), 160, single_line=True),
                    f"Invalid section or edition in class template {key}.")
            if (codes := slot.get("sectionCodes")) is not None:
                require(isinstance(codes, list) and 1 <= len(codes) <= 20
                        and all(isinstance(code, str) and re.fullmatch(r"T[1-9][0-9]{0,2}[A-Z]?", code) for code in codes)
                        and len(set(codes)) == len(codes)
                        and {int(code[1:].rstrip("ABCDEFGHIJKLMNOPQRSTUVWXYZ")) for code in codes} == set(numbers)
                        and (mode != "consecutive" or (len(codes) > 1 and len(numbers) == 1
                             and all(code[-1].isalpha() for code in codes))),
                        f"Invalid exact section codes in class template {key}.")


def read_class_template_document(stream, pdf):
    value = json.load(stream)
    require(isinstance(value, dict) and type(value.get("schemaVersion")) is int and value["schemaVersion"] == 1,
            "Unsupported class-templates.json schema.")
    templates = value["templates"]
    validate_class_templates(templates, pdf)
    require(bool(templates), "The class template file must contain at least one format.")
    return templates


def check_class_template_sources(path, templates, *, include_omissions=False, source_evidence=None):
    if not templates and not include_omissions:
        return []
    formats = class_format_suggestions(path, None if include_omissions else [template["page"] for template in templates])
    expected = {row["key"]: row for row in source_evidence["templates"]} if source_evidence else {}
    verified_pages = source_evidence["templatePages"] if source_evidence else {}
    resolved_pages = {page for pages in verified_pages.values() for page in pages}
    formats = [row for row in formats if not (row.get("sourceTrackReferences")
                                              and resolved_pages.intersection(row["sourcePages"]))]
    grouped = {}
    for index, item in enumerate(formats):
        for page in item.get("sourcePages", [item["page"]]):
            grouped.setdefault(page, []).append((index, item))
    manual = []
    matched = set()

    def rule(slot):
        return (slot["sectionNumbers"], slot.get("requiredDurationMinutes"),
                slot.get("sectionCodes"), slot.get("selectionMode", "single"), slot.get("isOptional", False),
                slot.get("variantPreference") if slot.get("enforcesVariant") else None)

    for template in templates:
        if template["key"] in expected:
            original = expected[template["key"]]
            require(template["page"] in verified_pages[template["key"]]
                    and template["minutes"] == original["minutes"]
                    and list(map(rule, template["slots"])) == list(map(rule, original["slots"])),
                    f"{template['name']} · 课长、来源页或课型顺序与原谱不符。")
            continue
        choices = grouped.get(template["page"], [])
        named = [(index, item) for index, item in choices if item.get("sourceTrackReferences")]
        course_name = " ".join(template["name"].casefold().split()) + " "
        named_matches = [(index, item) for index, item in named
                         if course_name.startswith(item["name"].casefold() + " ")]
        choices = [(index, item) for index, item in choices if not item.get("sourceTrackReferences")]
        if named_matches:
            verified_shape = [(index, item) for index, item in named_matches
                              if item["minutes"] == template["minutes"]
                              and len(item["sourceTrackReferences"]) == len(template["slots"])]
            require(verified_shape, f"{template['name']} · 课长或小节数量与原谱第 {template['page']} 页不符。")
            index, item = verified_shape[0]
            matched.add(index)
            references = "、".join(f"{ref['sourceCourse'].removeprefix('GRIT ')} "
                                  f"{ref['sourceRelease']} {ref['sourceTrack']}"
                                  for ref in item["sourceTrackReferences"])
            manual.append(f"{template['name']} · 原谱第 {template['page']} 页按来源课型核对曲目映射 · {references}")
            continue
        if not choices:
            manual.append(f"{template['name']} · 原谱第 {template['page']} 页需人工核对课型。")
            continue
        template_rules = list(map(rule, template["slots"]))
        matching = [index for index, item in choices if not item.get("sourceReview")
                    and item["minutes"] == template["minutes"] and list(map(rule, item["slots"])) == template_rules]
        if not matching and any(item["minutes"] == template["minutes"] and item.get("sourceReview") for _, item in choices):
            raise ValueError(f"{template['name']} · 原谱第 {template['page']} 页有当前模板无法完整表达的组合选曲。")
        require(matching, f"{template['name']} · 课长、选曲顺序或指定版本与原谱第 {template['page']} 页不符。")
        matched.add(next((index for index in matching if index not in matched), matching[0]))
    if include_omissions:
        provided = {template["key"] for template in templates}
        manual.extend(f"{row['name']} · 原谱第 {row['page']} 页的课型尚未写入包。"
                      for key, row in expected.items() if key not in provided)
        if source_evidence:
            manual.extend(source_evidence["issues"])
        for index, item in enumerate(formats):
            location = f"{item['name']} · 原谱第 {item['page']} 页"
            if item.get("sourceTrackReferences"):
                if index not in matched:
                    manual.append(f"{location} 有可识别课型，待核对来源课型与包内曲目后补入。")
            elif item.get("sourceReview"):
                manual.append(f"{location} 有组合选曲需人工复核 · {' / '.join(item['sourceReview'])}")
            elif index not in matched:
                manual.append(f"{location} 有可识别课型，包内未提供对应模板。")
    return manual


def validate_manifest(value):
    program, _, _ = read_identity(value)
    songs = value["music"]
    require(type(value["schemaVersion"]) is int and value["schemaVersion"] == 1
            and isinstance(songs, list) and 1 <= len(songs) <= 128, "Unsupported package manifest")
    validate_display_metadata(value)
    if "qualityNotes" in value:
        validate_quality_notes(value["qualityNotes"])
    validate_slots(program, songs)
    video, pdf, cover = (value.get(k) for k in ("video", "pdf", "cover"))
    assets = [item["asset"] for item in songs]
    allowed = [(AUDIO, 384)] * len(songs)
    if video:
        assets.append(video["asset"])
        allowed.append((VIDEO, 8192))
        require(0 < video["duration"] <= 43200, "Invalid video duration")
    if pdf:
        assets.append(pdf["asset"])
        allowed.append(({"pdf"}, MAX_PDF_MIB))
        require(type(pdf["pageCount"]) is int and 1 <= pdf["pageCount"] <= 2000, "Invalid PDF page count")
    if cover:
        assets.append(cover)
        allowed.append(({"png", "jpg"}, 8))
    if class_file := value.get("classTemplatesFile"):
        require(pdf and type(class_file["bytes"]) is int and class_file["bytes"] <= 256 * 1024,
                "Class template file requires an original PDF and is limited to 256 KiB.")
        assets.append(class_file)
        allowed.append(({"json"}, 1))
    for asset, (extensions, max_mib) in zip(assets, allowed):
        require(asset["ext"] in extensions and type(asset["bytes"]) is int and 0 < asset["bytes"] <= max_mib * MIB
                and re.fullmatch(r"[a-f0-9]{64}", asset["sha256"]) is not None, "Invalid media entry")
    for item in songs:
        require(type(item["duration"]) in (int, float) and 0 < item["duration"] <= 43200, "Invalid music duration")
        if (edition := item.get("classDurationMinutes")) is not None:
            require(type(edition) is int and 5 <= edition <= 180, "Invalid recording edition.")
        if (chapter := item.get("chapter")) is not None:
            require(video and 0 <= chapter["start"] < chapter["end"] <= video["duration"] and
                    chapter["evidence"] in {"audioMatched", "visuallyConfirmed", "approximate"},
                    "Invalid chapter timing")
            if chapter["evidence"] == "audioMatched":
                require(abs(chapter["end"] - chapter["start"] - item["duration"]) <= 0.25,
                        "Audio match is not a complete unit-rate video window")
        if (page := item.get("pdfPage")) is not None:
            require(pdf and type(page) is int and 1 <= page <= pdf["pageCount"],
                    f"Invalid original PDF opening page for {item['section']}")
        if (function := item.get("sectionFunction")) is not None:
            validate_section_function(function, pdf, item["section"])
        rows = item.get("cues")
        require(rows is None or (pdf and isinstance(rows, list) and 1 <= len(rows) <= 4096),
                "Cues require a bounded source PDF")
        previous = -1
        previous_block, block_time = 0, None
        known_blocks = set()
        for cue in rows or []:
            require(previous <= cue["time"] <= item["duration"] and cue["time"] >= 0 and
                    type(cue["page"]) is int and 1 <= cue["page"] <= pdf["pageCount"] and
                    cue["action"].strip(), f"Invalid cue for {item['section']}")
            repeated = cue.get("repeatsBlock")
            if (block := cue.get("block")) is not None:
                require(type(block) is int and previous_block < block <= 128 and previous < cue["time"],
                        f"Invalid block boundary for {item['section']}")
                require(repeated is None or (type(repeated) is int and repeated in known_blocks),
                        f"The repeat in {item['section']} must refer to an earlier marked block")
                known_blocks.add(block)
                previous_block, block_time = block, cue["time"]
            else:
                require(cue["time"] != block_time, f"Combine cues sharing a block's opening clock in {item['section']}")
                require(repeated is None, f"Add the repeat reference only at the opening of {item['section']}'s block")
            previous = cue["time"]
            for key in ("action", "music", "counts", "repetitions", "coaching"):
                if (text := cue.get(key)) is not None:
                    require(valid_text(text, 4096), f"Invalid cue field for {item['section']}")
            if level := cue.get("level"):
                require(all(0 <= level[channel] <= 1 for channel in ("red", "green", "blue")), "Invalid PDF level color")
    return assets


def chapter_preflight(songs, paths, video, video_duration):
    matches = [(song, path) for song, path in zip(songs, paths, strict=True)
               if song.get("chapter", {}).get("evidence") == "audioMatched"]
    if not matches:
        return []
    alignment = alignment_module()

    def inspect(item):
        song, path = item
        chapter = song["chapter"]
        label = f"{song['section']} · {song['variant']}"
        start, end = chapter["start"], chapter["end"]
        if not 0 <= start < end <= video_duration + 0.05:
            return f"{label} · 音画章节超出视频时长。"
        try:
            evidence = timing_windows(song, path, video, alignment)
        except (ValueError, OSError) as error:
            return f"{label} · 无法读取音画复测窗口（{error}）。"
        if evidence["status"] != "measured":
            return f"{label} · 音画复测未通过。{timing_failure_detail(evidence)}。"
        return None

    return [failure for failure in map_recordings(inspect, matches) if failure]



def package(args):
    started = perf_counter()
    review = read_json(args.review)
    config = review["plan"]
    program, routine, revision = read_identity(config)
    require((program, routine) == author_identity(program, routine),
            "Use canonical project and routine identifiers in the plan.")
    validate_confirmation(config)
    requested_target = getattr(args, "lufs", None)
    require(requested_target is None or -24 <= requested_target <= -12,
            "Select a loudness target between -24 and -12 LUFS")
    music_target = None if getattr(args, "keep_music", False) else requested_target
    repair_peaks = bool(getattr(args, "repair_music_peaks", False))
    require(not repair_peaks or music_target is None,
            "Choose peak repair with original audio or explicitly normalize to --lufs.")
    require(len(config["music"]) == len(review["tracks"]), "Review does not cover every music file")
    validate_slots(program, config["music"])
    require(review.get("programRoutine", [program, routine]) == [program, routine],
            "Project or routine changed. Update the plan and run analyze again.")
    for song, findings in zip(config["music"], review["tracks"]):
        require(findings.get("selection", source_selection(song)) == source_selection(song),
                f"Section, variant, catalogue slot or cue pages changed for {song['section']}. Run analyze again.")
    timelines = [approved_timeline(song, findings) for song, findings in zip(config["music"], review["tracks"])]
    source_pdf = review["pdf"] if config.get("pdf") else None
    functions = [approved_section_function(song, findings, source_pdf)
                 for song, findings in zip(config["music"], review["tracks"])]
    pdf_path = same_source(checked_pdf_path(config["pdf"]), source_pdf["source"]) if source_pdf else None
    declared_sections = [{**song, "sectionFunction": function} for song, function in zip(config["music"], functions) if function]
    grit = grit_source_evidence(pdf_path, config["music"], routine) if pdf_path and program == "GRIT" else None
    checked_functions = section_function_checks(pdf_path, declared_sections, grit)
    manual_functions = require_matching_section_functions(checked_functions)
    approved_templates = [item for item in review.get("classTemplates", []) if item.get("approved") is True]
    require(not any(item.get("sourceReview") for item in approved_templates),
            "Skip course formats marked sourceReview. Their printed choices do not fit this template schema.")
    templates = [{key: value for key, value in item.items() if key not in {"approved", "sourceReview"}}
                 for item in approved_templates]
    validate_class_templates(templates, source_pdf)
    check_class_template_sources(pdf_path, templates, source_evidence=grit)
    cover = selected_cover(config)
    if cover:
        require(review.get("cover") == cover, "Review no longer matches the confirmed cover image.")
    if args.video_quality == "original":
        require(args.video_max_height is None and args.video_bitrate_kbps is None and args.video_codec is None,
                "Choose efficient, ipad or compact before setting video encoding options")
    if args.video_max_height is not None:
        require(240 <= args.video_max_height <= 2160, "Choose a video height from 240 to 2160")
    if args.video_bitrate_kbps is not None:
        require(250 <= args.video_bitrate_kbps <= 12000, "Choose a video bitrate from 250 to 12000 kb/s")
    destination = args.output_dir
    destination.mkdir(parents=True, exist_ok=True)
    name = f"{program}_{routine}_{revision}.ssroutine"
    output = destination / name
    require(not output.exists(), f"Package already exists: {name}")
    with tempfile.TemporaryDirectory(dir=destination, prefix=".ssroutine-") as directory:
        work = Path(directory)
        manifest = {"schemaVersion": 1, "program": program, "routine": routine, "revision": revision, "music": []}
        for key in ("projectName", "routineName", "credit", "releaseDate", "qualityNotes"):
            if config.get(key) is not None:
                manifest[key] = config[key]
        files = []
        loudness = []
        preflight_problems = []
        video_quality = None
        preflight_seconds = 0.0
        print(f"{name} · 检查与处理 {len(timelines)} 首音乐", file=sys.stderr, flush=True)

        def prepare_audio(item):
            index, (song, findings, timeline, function) = item
            original = same_source(song["file"], findings["source"])
            path = original
            output = work / f"audio-{index}.m4a"
            if music_target is None:
                readings = measure(original)
                if repair_peaks:
                    path, levels = balance_audio(original, output, None, measured=readings)
                else:
                    lufs, peak = readings
                    levels = {"inputLUFS": lufs, "outputLUFS": lufs, "truePeakDBTP": peak, "gainDB": 0}
            else:
                path, levels = balance_audio(original, output, music_target)
            duration, _ = probe(path, "audio")
            row = {key: song[key] for key in ("section", "variant", "catalogTrackID", "title", "pdfPage") if key in song}
            if "pdfPage" not in row and findings.get("pdfOpeningPage") is not None:
                row["pdfPage"] = findings["pdfOpeningPage"]
            row.update(asset=findings["source"] if path == original else identity(path),
                       duration=round(duration, 6))
            if (edition := findings.get("classEdition")) and edition.get("approved") is True:
                row["classDurationMinutes"] = edition["minutes"]
            if (chapter := findings.get("chapter")) and chapter.get("approved") is True:
                row["chapter"] = {key: chapter[key] for key in ("start", "end", "evidence")}
                if chapter["evidence"] == "audioMatched":
                    row["chapter"]["end"] = round(chapter["start"] + duration, 6)
            if timeline:
                row["cues"] = timeline
            if function:
                row["sectionFunction"] = function
            return path, row, levels

        tracks = zip(config["music"], review["tracks"], timelines, functions)
        for path, song, levels in map_recordings(prepare_audio, enumerate(tracks)):
            files.append(path)
            manifest["music"].append(song)
            loudness.append({"section": song["section"], "variant": song["variant"], **levels})
            label = f"{song['section']} · {song['variant']}"
            if levels["truePeakDBTP"] > 0:
                preflight_problems.append(f"{label} · 实测真峰值 {levels['truePeakDBTP']:+.1f} dBTP，高于 0 dBTP。"
                                          "选择保留相对响度并仅降峰，或重新均衡。")
            if music_target is not None and abs(round(levels["outputLUFS"] - music_target, 2)) > 2:
                preflight_problems.append(f"{label} · 保护峰值后为 {levels['outputLUFS']:.1f} LUFS，"
                                          f"与目标 {music_target:.1f} LUFS 相差超过 2 LU。")
        audio_seconds = perf_counter() - started
        if config.get("video"):
            path = same_source(config["video"], review["video"]["source"])
            original_picture = video_info(path)
            if args.balance_video:
                video_target = DEFAULT_LOUDNESS_TARGET if requested_target is None else requested_target
                path, levels = balance_audio(path, work / "video.mp4", video_target, video=True)
                loudness.append({"section": "video", **levels})
            print(f"{name} · 编码前复测各小节音画时钟", file=sys.stderr, flush=True)
            check_started = perf_counter()
            preflight_problems.extend(chapter_preflight(manifest["music"], files, path, original_picture["duration"]))
            preflight_seconds = perf_counter() - check_started
        require(not preflight_problems,
                f"{name} 暂缓制作，尚未开始视频编码。\n" + "\n".join(preflight_problems))
        if config.get("video"):
            print(f"{name} · 处理视频（{args.video_quality}）", file=sys.stderr, flush=True)
            if args.video_quality != "original":
                height, bitrate = VIDEO_PROFILES[args.video_quality]
                encode = efficient_video if args.video_quality == "efficient" else optimize_video
                codec = args.video_codec or "hevc"
                path, encoded_picture = encode(path, work / "encoded.mp4",
                    args.video_max_height or height, args.video_bitrate_kbps or bitrate, codec)
                if encoded_picture["codec"] != codec:
                    name_for_codec = "H.265 (HEVC)" if codec == "hevc" else "H.264"
                    reason = encoded_picture.get("reason", "Output codec did not match.")
                    raise ValueError(f"Could not package {name_for_codec} video. {reason} "
                                     "Choose an iPad or compact profile to adjust the encode, "
                                     "or explicitly choose original quality to keep the source codec.")
            else:
                encoded_picture = video_info(path)
            video_quality = {"preset": args.video_quality, "source": original_picture, "output": encoded_picture}
            manifest["video"] = {"asset": identity(path), "duration": round(encoded_picture["duration"], 6)}
            files.append(path)
        video_seconds = perf_counter() - started - audio_seconds - preflight_seconds
        if pdf_path:
            manifest["pdf"] = {"asset": source_pdf["source"], "pageCount": source_pdf["pageCount"]}
            files.append(pdf_path)
        if cover:
            path = same_source(config["cover"], cover)
            manifest["cover"] = cover
            files.append(path)
        if templates:
            path = work / "class-templates.json"
            write_json(path, {"schemaVersion": 1, "templates": templates})
            manifest["classTemplatesFile"] = identity(path)
            files.append(path)
        assets = validate_manifest(manifest)
        encoded = json.dumps(manifest, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode()
        require(len(encoded) <= MAX_MANIFEST and 12 + len(encoded) + sum(a["bytes"] for a in assets) <= MAX_PACKAGE,
                "Package exceeds the format size limit")
        pending = work / name
        with pending.open("xb") as stream:
            os.chmod(pending, 0o600)
            stream.write(MAGIC + struct.pack(">I", len(encoded)) + encoded)
            for asset, file in zip(assets, files):
                with file.open("rb") as source:
                    consume(source, asset, destination=stream)
                    require(not source.read(1), f"Source grew while packing: {file.name}")
            stream.flush()
            os.fsync(stream.fileno())
        receipt = verify(pending, manual_functions=manual_functions)
        os.link(pending, output)
    receipt.update(output=str(output.resolve()), loudness=loudness, videoQuality=video_quality,
                   seconds=round(perf_counter() - started, 2), audioSeconds=round(audio_seconds, 2),
                   videoSeconds=round(video_seconds, 2), mediaPreflightSeconds=round(preflight_seconds, 2),
                   musicPolicy="keepOriginalDynamics" if music_target is None else "targetLoudness")
    print(json.dumps(receipt, ensure_ascii=False, indent=2))
    return output


def consume(stream, asset, destination=None, package_digest=None, mirror=None):
    remaining = asset["bytes"]
    observed = hashlib.sha256()
    while remaining:
        chunk = stream.read(min(MIB, remaining))
        require(bool(chunk), "Truncated media payload")
        observed.update(chunk)
        if package_digest is not None:
            package_digest.update(chunk)
        if destination is not None:
            destination.write(chunk)
        if mirror is not None:
            mirror.write(chunk)
        remaining -= len(chunk)
    require(observed.hexdigest() == asset["sha256"], "Media payload digest mismatch")


def read_manifest(stream):
    require(stream.read(8) == MAGIC, "Invalid package header")
    size = int.from_bytes(stream.read(4), "big")
    require(0 < size <= MAX_MANIFEST, "Invalid manifest length")
    encoded = stream.read(size)
    require(len(encoded) == size, "Truncated manifest")
    return encoded, json.loads(encoded)


def container_digest(encoded):
    return hashlib.sha256(MAGIC + struct.pack(">I", len(encoded)) + encoded)


def require_matching_section_functions(checks):
    contradictions = [message for status, message in checks if status == "contradiction"]
    require(not contradictions, "\n".join(contradictions))
    return [message for status, message in checks if status == "manual"]


def section_function_checks(pdf, songs, grit=None):
    songs = [song for song in songs if song.get("sectionFunction")]
    checked = [check_grit_section_function(song, grit) if song["variant"] == "standard" else None
               for song in songs]
    generic_songs = [song for song, result in zip(songs, checked) if result is None]
    generic = iter(inspect_section_functions(pdf, generic_songs)) if generic_songs else iter(())
    return [result if result is not None else next(generic) for result in checked]


def verify(path, *, manual_functions=None):
    before = path.stat()
    require(before.st_size <= MAX_PACKAGE, "Package too large")
    with (path.open("rb") as stream, tempfile.SpooledTemporaryFile(max_size=4 * MIB) as pdf,
          tempfile.SpooledTemporaryFile(max_size=MIB) as class_file):
        encoded, manifest = read_manifest(stream)
        package_digest = container_digest(encoded)
        assets = validate_manifest(manifest)
        template_asset = manifest.get("classTemplatesFile")
        checks_functions = manual_functions is None and any(item.get("sectionFunction") for item in manifest["music"])
        pdf_asset = manifest["pdf"]["asset"] if checks_functions or template_asset else None
        for asset in assets:
            mirror = pdf if asset is pdf_asset else class_file if asset is template_asset else None
            consume(stream, asset, package_digest=package_digest, mirror=mirror)
        require(not stream.read(1), "Trailing data in package")
        templates, manual_formats = [], []
        if template_asset:
            class_file.seek(0)
            templates = read_class_template_document(class_file, manifest["pdf"])
        if pdf_asset is not None:
            pdf.seek(0)
            grit = grit_source_evidence(pdf, manifest["music"], manifest["routine"]) if manifest["program"] == "GRIT" else None
            pdf.seek(0)
            manual_formats = check_class_template_sources(pdf, templates, source_evidence=grit)
            if checks_functions:
                pdf.seek(0)
                manual_functions = require_matching_section_functions(section_function_checks(pdf, manifest["music"], grit))
    after = path.stat()
    require((before.st_ino, before.st_size, before.st_mtime_ns) ==
            (after.st_ino, after.st_size, after.st_mtime_ns), "Package changed during verification")
    return {"file": path.name, "bytes": before.st_size, "sha256": package_digest.hexdigest(),
            "manifestSHA256": hashlib.sha256(encoded).hexdigest(),
            "releaseDate": manifest.get("releaseDate"),
            "music": len(manifest["music"]), "video": bool(manifest.get("video")), "pdf": bool(manifest.get("pdf")),
            "cover": bool(manifest.get("cover")), "chapters": sum("chapter" in m for m in manifest["music"]),
            "pdfOpeningPages": sum(m.get("pdfPage") is not None for m in manifest["music"]),
            "cueRows": sum(len(m.get("cues") or []) for m in manifest["music"]),
            "sectionFunctionManualReview": manual_functions or [],
            "classTemplates": len(templates),
            "classTemplateManualReview": manual_formats if templates else []}


def extract_manifest(args):
    with args.package.open("rb") as stream:
        _, manifest = read_manifest(stream)
    validate_manifest(manifest)
    write_json(args.output, manifest)


def recording_index(songs, selector):
    section, separator, variant = selector.partition(":")
    matches = [index for index, song in enumerate(songs)
               if song["section"] == section.upper() and (not separator or song["variant"] == variant)]
    require(len(matches) == 1, f"{selector} must identify one recording. Use SECTION:variant for a shared slot.")
    return matches[0]


def rebalance_recording(source, source_manifest, song, work, index, target):
    section = song["section"]
    source_songs = source_manifest["music"]
    asset = source_songs[index]["asset"]
    beginning = source.tell()
    offset = sum(row["asset"]["bytes"] for row in source_songs[:index])
    source.seek(beginning + offset)
    original = work / f"original-{index}.{asset['ext']}"
    with original.open("xb") as stream:
        consume(source, asset, destination=stream)
    source.seek(beginning)
    output, levels = balance_audio(original, work / f"balanced-{index}.m4a", target)
    duration, _ = probe(output, "audio")
    require(abs(duration - song["duration"]) <= 0.20, f"{section} duration changed beyond tolerance")
    song.update(asset=identity(output), duration=round(duration, 6))
    if (chapter := song.get("chapter")) and chapter["evidence"] == "audioMatched":
        chapter["end"] = round(chapter["start"] + duration, 6)
    return output, levels


def revise(args):
    updated = read_json(args.manifest)
    program, routine, _ = read_identity(updated)
    require((program, routine) == author_identity(program, routine), "Use canonical project and routine identifiers")
    require(args.revision > updated["revision"], "Use a higher revision than the package being corrected")
    updated["revision"] = args.revision
    reviewed_assets = validate_manifest(updated)
    for song in updated["music"]:
        require(not unresolved_repeat_times(song.get("cues") or []),
                f"Finish structural repeats before submitting {song['section']}")
    replacing_cover = args.cover is not None or args.without_cover
    if replacing_cover:
        if args.cover is not None:
            updated["cover"] = cover_asset(args.cover)
        else:
            updated.pop("cover", None)
    if args.class_templates:
        with args.class_templates.open("rb") as document:
            read_class_template_document(document, updated.get("pdf"))
        updated["classTemplatesFile"] = identity(args.class_templates)
    args.output_dir.mkdir(parents=True, exist_ok=True)
    name = f"{program}_{routine}_{args.revision}.ssroutine"
    output = args.output_dir / name
    require(not output.exists(), f"Package already exists: {name}")
    with args.package.open("rb") as source, tempfile.TemporaryDirectory(
            dir=args.output_dir, prefix=".ssroutine-revision-") as directory:
        _, old = read_manifest(source)
        require((program, routine) == (old["program"], old["routine"]) and args.revision > old["revision"],
                "A revision must retain the package identity and increase its version")
        old_assets = validate_manifest(old)
        require(old_assets == reviewed_assets, "Changing media requires building a new package from source")
        replacements = {}
        repairs = []
        normalizing = getattr(args, "rebalance", None) or []
        reducing_peaks = getattr(args, "reduce_peak", None) or []
        if isinstance(normalizing, str):
            normalizing = [normalizing]
        require(not normalizing or -24 <= args.lufs <= -12, "Select a loudness target between -24 and -12 LUFS")
        selections = [(item, args.lufs) for item in normalizing] + [(item, None) for item in reducing_peaks]
        for selector, target in selections:
            index = recording_index(updated["music"], selector)
            require(index not in replacements, f"{selector} was selected more than once.")
            audio, levels = rebalance_recording(source, old, updated["music"][index], Path(directory), index, target)
            replacements[index] = audio
            repairs.append({"recording": selector, "policy": "peakOnly" if target is None else "targetLoudness", **levels})
        assets = validate_manifest(updated)
        encoded = json.dumps(updated, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode()
        require(0 < len(encoded) <= MAX_MANIFEST and
                12 + len(encoded) + sum(asset["bytes"] for asset in assets) <= MAX_PACKAGE,
                "Package exceeds the format size limit")
        temporary = Path(directory) / name
        with temporary.open("xb") as stream:
            os.chmod(temporary, 0o600)
            stream.write(MAGIC + struct.pack(">I", len(encoded)) + encoded)
            media_assets = old_assets[:len(old["music"]) + bool(old.get("video")) + bool(old.get("pdf"))]
            for index, asset in enumerate(media_assets):
                if index in replacements:
                    consume(source, asset)
                    with replacements[index].open("rb") as audio:
                        consume(audio, assets[index], destination=stream)
                        require(not audio.read(1), "Balanced audio changed while packing")
                else:
                    consume(source, asset, destination=stream)
            if original_cover := old.get("cover"):
                consume(source, original_cover, destination=None if replacing_cover else stream)
            if args.cover is not None:
                with args.cover.open("rb") as artwork:
                    consume(artwork, updated["cover"], destination=stream)
                    require(not artwork.read(1), "Cover changed while packing")
            if original_template := old.get("classTemplatesFile"):
                consume(source, original_template, destination=None if args.class_templates else stream)
            if args.class_templates:
                with args.class_templates.open("rb") as document:
                    consume(document, updated["classTemplatesFile"], destination=stream)
                    require(not document.read(1), "Class template file changed while packing")
            require(not source.read(1), "Trailing data in the source package")
            stream.flush()
            os.fsync(stream.fileno())
        receipt = verify(temporary)
        os.link(temporary, output)
    if repairs:
        receipt["musicRepairs"] = repairs
        if len(repairs) == 1 and repairs[0]["policy"] == "targetLoudness":
            receipt["rebalanced"] = {key: value for key, value in repairs[0].items() if key != "policy"}
    print(json.dumps(receipt, ensure_ascii=False, indent=2))


def doctor():
    import numpy
    import pdfplumber

    for name in ("ffmpeg", "ffprobe"):
        require(shutil.which(name), f"Install {name} and add it to the agent terminal's PATH")
    alignment_module()
    print(json.dumps({"python": sys.version.split()[0], "numpy": numpy.__version__,
                      "pdfplumber": pdfplumber.__version__,
                      "ocr": all(shutil.which(name) for name in ("pdftoppm", "tesseract")),
                      "screenSubtitleOCR": bool(shutil.which("tesseract"))}))


def add_media_options(command):
    command.add_argument("--lufs", type=float, metavar="LUFS",
                         help="Explicitly normalize each music recording to LUFS")
    command.add_argument("--keep-music", action="store_true",
                         help="Preserve original music (default); takes precedence over --lufs for music")
    command.add_argument("--repair-music-peaks", action="store_true",
                         help="Attenuate only recordings above 0 dBTP while otherwise keeping original music")
    command.add_argument("--balance-video", action="store_true",
                         help="Explicitly normalize the video audio track to --lufs or -16 LUFS")
    command.add_argument("--video-quality", choices=("efficient", "original", "ipad", "compact"), default="efficient")
    command.add_argument("--video-max-height", type=int, help="Override the selected preset's height limit")
    command.add_argument("--video-bitrate-kbps", type=int, help="Override the selected preset's video bitrate")
    command.add_argument("--video-codec", choices=("hevc", "h264"), help="Require this codec when encoding (default: HEVC)")


def add_delivery_location(command):
    location = command.add_mutually_exclusive_group(required=True)
    location.add_argument("--workspace", type=Path, help="Current project's workspace; deliver under its 交付 folder")
    location.add_argument("--output-dir", type=Path, help="Delivery folder explicitly chosen for this batch")


def review_problems(review, *, source_limited=False):
    plan = review["plan"]
    pdf, video = bool(plan.get("pdf")), bool(plan.get("video"))
    problems = []
    if not source_limited:
        if not pdf:
            problems.append("未选原谱。若原始资料确实没有，请先确认课程的交付范围。")
        if not video:
            problems.append("未选视频。若原始资料确实没有，请先确认课程的交付范围。")
    for song, record in zip(plan["music"], review["tracks"], strict=True):
        section = song["section"]
        supplement = record.get("supplementary") is True
        chapter = record.get("chapter") or {}
        if video and not supplement and (chapter.get("evidence") != "audioMatched"
                                         or chapter.get("approved") is not True):
            problems.append(f"{section} · 教学视频的曲内时钟未核实。")
        if pdf and (record.get("sectionFunction") or {}).get("approved") is not True:
            problems.append(f"{section} · 尚未核实小节功能。")
        cues = record.get("cues") or {}
        if pdf and not (song.get("pdfPage") or record.get("pdfOpeningPage") or cues.get("rows")):
            problems.append(f"{section} · 尚未指定原谱入口。")
        has_clock_evidence = cues.get("rows") or record.get("intervalReview")
        if (pdf and not supplement and (has_clock_evidence or not source_limited)
                and (not cues.get("rows") or cues.get("approved") is not True)):
            problems.append(f"{section} · 补齐并核实动作时间线。")
        elif proposal := record.get("intervalReview"):
            clock = record.get("intervalClock") or {}
            anchor = clock.get("anchorSeconds")
            positions = set()
            if type(anchor) in (int, float) and math.isfinite(anchor):
                for observation in clock.get("observations", ()):
                    music_second = observation.get("musicSecond")
                    score_second = observation.get("scoreSecond")
                    if (type(music_second) in (int, float) and type(score_second) in (int, float)
                            and 0 <= music_second < record["duration"]
                            and abs(music_second - anchor - score_second) <= 1.25):
                        positions.add(min(2, int(music_second / record["duration"] * 3)))
            if positions != {0, 1, 2}:
                problems.append(f"{section} · 核实开头、中段、尾段的原谱与曲内时钟，再确认分段时间。")
            else:
                status, reason = validate_interval_review(proposal, cues.get("rows") or [],
                                                          observed_anchor_seconds=anchor)
                if status != "matched":
                    problems.append(f"{section} · {reason}")
    for template in review.get("classTemplates", ()):
        if template.get("approved") is not True:
            problems.append(f"{template['name']} · 核实原谱顺序及实际录音后再确认课型。")
    problems.extend(review.get("sourceStructureWarnings", ()))
    return list(dict.fromkeys(problems))


def check_review(args):
    review = read_json(args.review)
    blockers = review_problems(review, source_limited=args.source_limited)
    if getattr(args, "measure_video", False) and review["plan"].get("video"):
        video = same_source(review["plan"]["video"], review["video"]["source"])
        songs, paths = [], []
        for song, record in zip(review["plan"]["music"], review["tracks"], strict=True):
            chapter = record.get("chapter") or {}
            if chapter.get("approved") is True and chapter.get("evidence") == "audioMatched":
                paths.append(same_source(song["file"], record["source"]))
                songs.append({**song, "duration": record["duration"], "chapter": chapter})
        blockers.extend(chapter_preflight(songs, paths, video, review["video"]["duration"]))
    print(json.dumps({"readyToBuild": not blockers, "blockers": blockers}, ensure_ascii=False, indent=2))
    if blockers:
        raise SystemExit(2)


def finish(args):
    from batch_submission import delivery_batch_id, submit

    started = perf_counter()
    require(1 <= len(args.reviews) <= 256, "Select between 1 and 256 reviewed routines")
    require(getattr(args, "screen_subtitles", None) is None or len(args.reviews) == 1,
            "An explicit subtitle observation applies to one inspected package")
    args.batch_id = delivery_batch_id(args)
    for review in args.reviews:
        blockers = review_problems(read_json(review), source_limited=getattr(args, "source_limited", False))
        require(not blockers, f"{review.name} 暂缓交付，尚未开始编码。\n" + "\n".join(blockers))
    work = Path(tempfile.mkdtemp(prefix="ssroutine-production-"))
    outputs = []
    try:
        for review in args.reviews:
            options = argparse.Namespace(**vars(args))
            options.review = review
            options.output_dir = work
            outputs.append(package(options))
        args.packages = outputs
        submit(args)
    except BaseException:
        if outputs:
            print(f"已完成的资源包保存在 {work}；可检查后用 submit 继续交付。", file=sys.stderr)
        else:
            work.rmdir()
        raise
    shutil.rmtree(work)
    print(f"制作与质检合计 {perf_counter() - started:.1f}s", file=sys.stderr)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("doctor")
    p = commands.add_parser("plan")
    p.add_argument("source", type=Path)
    p.add_argument("--program", required=True)
    p.add_argument("--routine", required=True)
    p.add_argument("--revision", default=1, type=int)
    p.add_argument("--project-name")
    p.add_argument("--routine-name")
    credit = p.add_mutually_exclusive_group()
    credit.add_argument("--credit", type=public_credit, help="Confirmed public creator name")
    credit.add_argument("--no-credit", dest="credit", action="store_const", const="")
    p.add_argument("--release-date", help="A confirmed release date or quarter, such as 2024Q3")
    p.add_argument("--video-note", help="Known reason for intentionally missing or unlocated video")
    p.add_argument("--timeline-note", help="Known reason for an omitted action timeline")
    cover = p.add_mutually_exclusive_group()
    cover.add_argument("--cover", type=Path, help="A JPEG or PNG confirmed by the creator")
    cover.add_argument("--without-cover", action="store_true")
    p.add_argument("--output", required=True, type=Path)
    p = commands.add_parser("confirm-plan", help="Record approved display names, attribution and artwork")
    p.add_argument("input", type=Path)
    p.add_argument("--project-name", required=True)
    p.add_argument("--routine-name", required=True)
    credit = p.add_mutually_exclusive_group()
    credit.add_argument("--credit", type=public_credit, help="Use this public creator name for this package")
    credit.add_argument("--no-credit", dest="credit", action="store_const", const="")
    p.add_argument("--remember-credit", action="store_true", help="Save this attribution choice as the local default")
    cover = p.add_mutually_exclusive_group()
    cover.add_argument("--cover", type=Path)
    cover.add_argument("--without-cover", action="store_true")
    p.add_argument("--output", required=True, type=Path)
    p = commands.add_parser("text-cover", help="Preview a square cover using the course and routine names")
    p.add_argument("input", type=Path, help="Routine plan with the proposed display names")
    p.add_argument("--project-name", help="Course name for the preview")
    p.add_argument("--routine-name", help="Routine name for the preview")
    p.add_argument("--font", type=Path, help="Font file when the system font lacks the needed characters")
    p.add_argument("--output", required=True, type=Path)
    p = commands.add_parser("attribution", help="Show or reset the local default attribution choice")
    p.add_argument("--reset", action="store_true")
    p = commands.add_parser("select-cover", help="Record the creator's artwork selection in a plan or review")
    p.add_argument("input", type=Path)
    choice = p.add_mutually_exclusive_group(required=True)
    choice.add_argument("--cover", type=Path, help="The confirmed artwork file")
    choice.add_argument("--without-cover", action="store_true", help="The creator chose to omit artwork")
    p.add_argument("--output", required=True, type=Path)
    p = commands.add_parser("analyze")
    p.add_argument("plan", type=Path)
    p.add_argument("--output", required=True, type=Path)
    p.add_argument("--ocr", action="store_true")
    p.add_argument("--skip-alignment", action="store_true")
    p = commands.add_parser("build")
    p.add_argument("review", type=Path)
    p.add_argument("--output-dir", required=True, type=Path)
    add_media_options(p)
    p = commands.add_parser("finish", help="Build reviewed routines and seal one verified submission in a single run")
    p.add_argument("reviews", type=Path, nargs="+")
    add_delivery_location(p)
    p.add_argument("--batch-id")
    p.add_argument("--font", type=Path, help="CJK font for the visual QA cards")
    p.add_argument("--source-limited", action="store_true",
                   help="Deliver a creator-confirmed limited scope when original materials are genuinely absent")
    p.add_argument("--screen-subtitles", choices=("present", "absent"),
                   help="Visual judgment after examining the frames of the output video")
    add_media_options(p)
    p = commands.add_parser("check-review", help="List unfinished source checks before encoding")
    p.add_argument("review", type=Path)
    p.add_argument("--source-limited", action="store_true")
    p.add_argument("--measure-video", action="store_true",
                   help="Remeasure approved video chapters against the selected source files")
    p = commands.add_parser("verify")
    p.add_argument("package", type=Path)
    p = commands.add_parser("manifest", help="Extract a private editable copy of a package manifest")
    p.add_argument("package", type=Path)
    p.add_argument("--output", required=True, type=Path)
    p = commands.add_parser("revise", help="Create a higher version using the verified original media bytes")
    p.add_argument("package", type=Path)
    p.add_argument("manifest", type=Path)
    p.add_argument("--revision", required=True, type=int)
    p.add_argument("--output-dir", required=True, type=Path)
    cover_options = p.add_mutually_exclusive_group()
    cover_options.add_argument("--cover", type=Path, help="Use an explicitly approved replacement cover")
    cover_options.add_argument("--without-cover", action="store_true", help="Remove the previous cover")
    p.add_argument("--class-templates", type=Path, help="Reviewed class-templates.json file to add or replace")
    p.add_argument("--rebalance", action="append", metavar="SECTION[:VARIANT]",
                   help="Normalize this recording, leaving other media unchanged. Repeat to select more.")
    p.add_argument("--reduce-peak", action="append", metavar="SECTION[:VARIANT]",
                   help="Attenuate this recording only if its peak exceeds 0 dBTP. Repeat to select more.")
    p.add_argument("--lufs", type=float, default=DEFAULT_LOUDNESS_TARGET, help="Target used by --rebalance (default: -16 LUFS)")
    p = commands.add_parser("submit", help="Create a clean batch with verified packages, QA images and fingerprints")
    p.add_argument("packages", type=Path, nargs="+")
    add_delivery_location(p)
    p.add_argument("--batch-id")
    p.add_argument("--font", type=Path, help="CJK font for the visual QA cards")
    p.add_argument("--source-limited", action="store_true",
                   help="Deliver a creator-confirmed limited scope when original materials are genuinely absent")
    p.add_argument("--screen-subtitles", choices=("present", "absent"),
                   help="Visual judgment after examining the samples of a single exact package")
    loudness = p.add_mutually_exclusive_group()
    loudness.add_argument("--keep-music", action="store_true",
                          help="Use original audio as the reference (default); verify decoding and peaks")
    loudness.add_argument("--target-lufs", type=float,
                          help="Verify against the producer's chosen loudness target")
    args = parser.parse_args()
    try:
        if args.command in {"finish", "submit"} and args.workspace is not None:
            workspace = args.workspace.expanduser().resolve(strict=True)
            require(workspace.is_dir(), "Choose the current project's workspace folder")
            args.output_dir = workspace / "交付"
            require(not args.output_dir.is_symlink(), "The workspace delivery folder points elsewhere. Confirm the destination explicitly.")
        if args.command == "doctor":
            doctor()
        elif args.command == "verify":
            print(json.dumps(verify(args.package), ensure_ascii=False, indent=2))
        elif args.command == "submit":
            from batch_submission import submit
            submit(args)
        else:
            {"plan": plan, "confirm-plan": confirm_plan, "text-cover": text_cover, "attribution": attribution,
             "select-cover": select_cover, "analyze": analyze, "build": package,
             "manifest": extract_manifest, "revise": revise, "finish": finish, "check-review": check_review}[args.command](args)
    except (ValueError, KeyError, TypeError, OSError, ImportError, subprocess.SubprocessError) as error:
        parser.exit(1, f"{error}\n")


if __name__ == "__main__":
    main()
