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

from media_analysis import (alignment_module, audio_packet_hash, balance_audio, efficient_video, extract_cues,
                            locate_chapters, map_recordings, optimize_video, probe, run, timing_windows,
                            unresolved_repeat_times, video_info)


MAGIC = b"SSRT0001"
MIB = 1024 * 1024
MAX_MANIFEST = 4 * MIB
MAX_PACKAGE = 12 * 1024 * MIB
AUDIO = {"aac", "aif", "aiff", "flac", "m4a", "mp3", "wav"}
VIDEO = {"mp4", "m4v", "mov"}
PICTURE = {"jpg", "jpeg", "png"}
VARIANTS = {"standard", "bonus", "alternative", "combo45", "combo45Alternative", "combo45Bonus", "outro", "preClass"}
PROGRAM_ALBUMS = {
    "BODYCOMBAT": "BC", "BODYJAM": "BJ", "BODYPUMP": "BP", "BODYATTACK": "BA", "RPM": "RPM",
    "BC": "BC", "BJ": "BJ", "BP": "BP", "BA": "BA", "CORE": "CORE", "TONE": "TONE", "GRIT": "GRIT",
}
PROGRAM_ALIASES = {**PROGRAM_ALBUMS, **{f"LESMILLS{name}": code for name, code in PROGRAM_ALBUMS.items()},
                   "莱美核心": "CORE", "莱美核心训练": "CORE", "莱美GRIT": "GRIT", "莱美TONE": "TONE",
                   "莱美有氧": "BA", "莱美有氧训练": "BA"}
NATIVE_PROGRAMS = {"BC", "BJ", "BP", "RPM"}
VIDEO_PROFILES = {"efficient": (None, None), "ipad": (1080, 1800), "compact": (720, 950)}


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


def cover_extension(source):
    from PIL import Image

    try:
        with Image.open(source) as image:
            ext = {"JPEG": "jpg", "PNG": "png"}.get(image.format)
            require(ext and all(1 <= size <= 8192 for size in image.size),
                    "Choose a JPEG or PNG image up to 8192 pixels per side.")
            image.load()
            return ext
    except (OSError, Image.DecompressionBombError) as error:
        raise ValueError("The cover image cannot be decoded.") from error


def cover_asset(path):
    require(path.is_file(), "Choose a local cover image.")
    with path.open("rb") as artwork:
        data = artwork.read(8 * MIB + 1)
    require(0 < len(data) <= 8 * MIB, "Choose a cover image of at most 8 MiB.")
    ext = cover_extension(BytesIO(data))
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
    asset = cover_asset(path)
    config["cover"] = str(path)
    config["coverSelection"] = {"sha256": asset["sha256"]}
    return asset


def selected_cover(config):
    selected = config.get("coverSelection")
    if path := config.get("cover"):
        require(selected and selected.get("sha256"),
                "Show the artwork to the creator and run select-cover with the confirmed image.")
        asset = cover_asset(Path(path))
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
    asset = set_cover_selection(config, args.cover)
    if config is not document:
        if asset:
            document["cover"] = asset
        else:
            document.pop("cover", None)
    write_json(args.output, document)


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
    value = {"program": program, "routine": routine, "revision": args.revision, "music": []}
    if args.credit:
        value["credit"] = args.credit
    if args.release_date:
        value["releaseDate"] = args.release_date
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
            match = re.match(r"^(?:(ALTERNATIVE|ALT|BNS|BONUS|替换曲|替换|备选|A|B)[ ._-]*)?(?:T[ ._-]*)?(\d{1,3})([A-Z]?)(?:[ ._-]+(.*)|$)",
                             stem, re.I)
            if not match or not 1 <= int(match[2]) <= 128:
                review.append(str(file))
                continue
            prefix, number, letter, title = match.groups()
            markers = {"ALT": "alternative", "ALTERNATIVE": "alternative", "A": "alternative",
                       "替换曲": "alternative", "替换": "alternative", "备选": "alternative",
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
    if args.cover:
        set_cover_selection(value, args.cover)
    else:
        value["coverCandidates"] = list_cover_candidates(cover_images, value["music"], embedded, args.output)
    value["reviewSources"] = review
    value["musicWarnings"] = warnings
    write_json(args.output, value)
    print(json.dumps({"musicProposed": len(value["music"]), "reviewSources": len(review),
                      "musicWarnings": len(warnings),
                      "coverCandidates": len(value.get("coverCandidates", []))}, ensure_ascii=False))


def analyze(args):
    started = perf_counter()
    config = read_json(args.plan)
    program, _, _ = read_identity(config)
    cover = selected_cover(config)
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
    if config.get("pdf"):
        pdf = Path(config["pdf"]).resolve(strict=True)
        page_count, cues, warnings = extract_cues(pdf, audio, ocr=args.ocr)
        review.update(pdf={"source": identity(pdf), "pageCount": page_count}, pdfWarnings=warnings)
        for record, rows in zip(review["tracks"], cues):
            record["cues"] = rows
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


def validate_manifest(value):
    program, _, _ = read_identity(value)
    songs = value["music"]
    require(type(value["schemaVersion"]) is int and value["schemaVersion"] == 1
            and isinstance(songs, list) and 1 <= len(songs) <= 128, "Unsupported package manifest")
    if (credit := value.get("credit")) is not None:
        require(credit and valid_text(credit, 128, single_line=True), "Invalid contributor credit")
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
        allowed.append(({"pdf"}, 64))
        require(type(pdf["pageCount"]) is int and 1 <= pdf["pageCount"] <= 2000, "Invalid PDF page count")
    if cover:
        assets.append(cover)
        allowed.append(({"png", "jpg"}, 8))
    for asset, (extensions, max_mib) in zip(assets, allowed):
        require(asset["ext"] in extensions and type(asset["bytes"]) is int and 0 < asset["bytes"] <= max_mib * MIB
                and re.fullmatch(r"[a-f0-9]{64}", asset["sha256"]) is not None, "Invalid media entry")
    for item in songs:
        require(type(item["duration"]) in (int, float) and 0 < item["duration"] <= 43200, "Invalid music duration")
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


def package(args):
    started = perf_counter()
    review = read_json(args.review)
    config = review["plan"]
    program, routine, revision = read_identity(config)
    require((program, routine) == author_identity(program, routine),
            "Use canonical project and routine identifiers in the plan.")
    require(-24 <= args.lufs <= -12, "Select a loudness target between -24 and -12 LUFS")
    require(len(config["music"]) == len(review["tracks"]), "Review does not cover every music file")
    validate_slots(program, config["music"])
    require(review.get("programRoutine", [program, routine]) == [program, routine],
            "Project or routine changed. Update the plan and run analyze again.")
    for song, findings in zip(config["music"], review["tracks"]):
        require(findings.get("selection", source_selection(song)) == source_selection(song),
                f"Section, variant, catalogue slot or cue pages changed for {song['section']}. Run analyze again.")
    timelines = [approved_timeline(song, findings) for song, findings in zip(config["music"], review["tracks"])]
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
        for key in ("credit", "releaseDate"):
            if key in config:
                manifest[key] = config[key]
        files = []
        loudness = []
        video_quality = None
        print(f"{name} · 检查与处理 {len(timelines)} 首音乐", file=sys.stderr, flush=True)

        def prepare_audio(item):
            index, (song, findings, timeline) = item
            path = same_source(song["file"], findings["source"])
            levels = None
            if not args.keep_music:
                path, levels = balance_audio(path, work / f"audio-{index}.m4a", args.lufs)
            duration, _ = probe(path, "audio")
            row = {key: song[key] for key in ("section", "variant", "catalogTrackID", "title", "pdfPage") if key in song}
            row.update(asset=identity(path), duration=round(duration, 6))
            if (chapter := findings.get("chapter")) and chapter.get("approved") is True:
                row["chapter"] = {key: chapter[key] for key in ("start", "end", "evidence")}
                if chapter["evidence"] == "audioMatched":
                    row["chapter"]["end"] = round(chapter["start"] + duration, 6)
            if timeline:
                row["cues"] = timeline
            return path, row, levels

        for path, song, levels in map_recordings(prepare_audio, enumerate(zip(config["music"], review["tracks"], timelines))):
            files.append(path)
            manifest["music"].append(song)
            if levels is not None:
                loudness.append({"section": song["section"], **levels})
        audio_seconds = perf_counter() - started
        if config.get("video"):
            print(f"{name} · 处理视频（{args.video_quality}）", file=sys.stderr, flush=True)
            path = same_source(config["video"], review["video"]["source"])
            original_picture = video_info(path)
            changed_matches = [(song, audio) for song, audio, finding in zip(manifest["music"], files, review["tracks"])
                               if song.get("chapter", {}).get("evidence") == "audioMatched"
                               and song["asset"] != finding["source"]]
            if changed_matches:
                alignment = alignment_module()

                def verify_balanced_clock(item):
                    song, audio = item
                    evidence = timing_windows(song, audio, path, alignment)
                    return None if evidence["status"] == "measured" else song["section"]

                failed = [slot for slot in map_recordings(verify_balanced_clock, changed_matches) if slot]
                require(not failed, f"{', '.join(failed)} · 音量处理后音画复测未通过。核对原始录音，或保留原音乐。")
            if args.balance_video:
                path, levels = balance_audio(path, work / "video.mp4", args.lufs, video=True)
                loudness.append({"section": "video", **levels})
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
        video_seconds = perf_counter() - started - audio_seconds
        if config.get("pdf"):
            path = same_source(config["pdf"], review["pdf"]["source"])
            manifest["pdf"] = {"asset": identity(path), "pageCount": review["pdf"]["pageCount"]}
            files.append(path)
        if cover:
            path = same_source(config["cover"], cover)
            manifest["cover"] = cover
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
        receipt = verify(pending)
        os.link(pending, output)
    receipt.update(output=str(output.resolve()), loudness=loudness, videoQuality=video_quality,
                   seconds=round(perf_counter() - started, 2), audioSeconds=round(audio_seconds, 2),
                   videoSeconds=round(video_seconds, 2))
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


def verify(path):
    before = path.stat()
    require(before.st_size <= MAX_PACKAGE, "Package too large")
    with path.open("rb") as stream:
        encoded, manifest = read_manifest(stream)
        package_digest = container_digest(encoded)
        for asset in validate_manifest(manifest):
            consume(stream, asset, package_digest=package_digest)
        require(not stream.read(1), "Trailing data in package")
    after = path.stat()
    require((before.st_ino, before.st_size, before.st_mtime_ns) ==
            (after.st_ino, after.st_size, after.st_mtime_ns), "Package changed during verification")
    return {"file": path.name, "bytes": before.st_size, "sha256": package_digest.hexdigest(),
            "manifestSHA256": hashlib.sha256(encoded).hexdigest(),
            "releaseDate": manifest.get("releaseDate"),
            "music": len(manifest["music"]), "video": bool(manifest.get("video")), "pdf": bool(manifest.get("pdf")),
            "cover": bool(manifest.get("cover")), "chapters": sum("chapter" in m for m in manifest["music"]),
            "pdfOpeningPages": sum(m.get("pdfPage") is not None for m in manifest["music"]),
            "cueRows": sum(len(m.get("cues") or []) for m in manifest["music"])}


def extract_manifest(args):
    with args.package.open("rb") as stream:
        _, manifest = read_manifest(stream)
    validate_manifest(manifest)
    write_json(args.output, manifest)


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
    assets = validate_manifest(updated)
    encoded = json.dumps(updated, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode()
    require(0 < len(encoded) <= MAX_MANIFEST and
            12 + len(encoded) + sum(asset["bytes"] for asset in assets) <= MAX_PACKAGE,
            "Package exceeds the format size limit")
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
        temporary = Path(directory) / name
        with temporary.open("xb") as stream:
            os.chmod(temporary, 0o600)
            stream.write(MAGIC + struct.pack(">I", len(encoded)) + encoded)
            media_assets = old_assets[:-1] if replacing_cover and old.get("cover") else old_assets
            for asset in media_assets:
                consume(source, asset, destination=stream)
            if replacing_cover and old.get("cover"):
                consume(source, old["cover"])
            require(not source.read(1), "Trailing data in the source package")
            if args.cover is not None:
                with args.cover.open("rb") as artwork:
                    consume(artwork, updated["cover"], destination=stream)
                    require(not artwork.read(1), "Cover changed while packing")
            stream.flush()
            os.fsync(stream.fileno())
        receipt = verify(temporary)
        os.link(temporary, output)
    print(json.dumps(receipt, ensure_ascii=False, indent=2))


def doctor():
    import numpy
    import pdfplumber

    for name in ("ffmpeg", "ffprobe"):
        require(shutil.which(name), f"Install {name} and add it to the agent terminal's PATH")
    alignment_module()
    print(json.dumps({"python": sys.version.split()[0], "numpy": numpy.__version__,
                      "pdfplumber": pdfplumber.__version__,
                      "ocr": all(shutil.which(name) for name in ("pdftoppm", "tesseract"))}))


def add_media_options(command):
    command.add_argument("--lufs", type=float, default=-16.0)
    command.add_argument("--keep-music", action="store_true")
    command.add_argument("--balance-video", action="store_true")
    command.add_argument("--video-quality", choices=("efficient", "original", "ipad", "compact"), default="efficient")
    command.add_argument("--video-max-height", type=int, help="Override the selected preset's height limit")
    command.add_argument("--video-bitrate-kbps", type=int, help="Override the selected preset's video bitrate")
    command.add_argument("--video-codec", choices=("hevc", "h264"), help="Require this codec when encoding (default: HEVC)")


def finish(args):
    from batch_submission import submit

    started = perf_counter()
    require(1 <= len(args.reviews) <= 256, "Select between 1 and 256 reviewed routines")
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
    p.add_argument("--credit")
    p.add_argument("--release-date", help="A confirmed release date or quarter, such as 2024Q3")
    p.add_argument("--cover", type=Path, help="A JPEG or PNG confirmed by the creator")
    p.add_argument("--output", required=True, type=Path)
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
    p.add_argument("--output-dir", required=True, type=Path)
    p.add_argument("--batch-id")
    p.add_argument("--font", type=Path, help="CJK font for the visual QA cards")
    add_media_options(p)
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
    p = commands.add_parser("submit", help="Create a clean batch with verified packages, QA images and fingerprints")
    p.add_argument("packages", type=Path, nargs="+")
    p.add_argument("--output-dir", required=True, type=Path)
    p.add_argument("--batch-id")
    p.add_argument("--font", type=Path, help="CJK font for the visual QA cards")
    args = parser.parse_args()
    try:
        if args.command == "doctor":
            doctor()
        elif args.command == "verify":
            print(json.dumps(verify(args.package), ensure_ascii=False, indent=2))
        elif args.command == "submit":
            from batch_submission import submit
            submit(args)
        else:
            {"plan": plan, "select-cover": select_cover, "analyze": analyze, "build": package,
             "manifest": extract_manifest, "revise": revise, "finish": finish}[args.command](args)
    except (ValueError, KeyError, TypeError, OSError, ImportError, subprocess.SubprocessError) as error:
        parser.exit(1, f"{error}\n")


if __name__ == "__main__":
    main()
