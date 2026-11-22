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
from pathlib import Path

from media_analysis import (alignment_module, audio_packet_hash, balance_audio, extract_cues, locate_chapters,
                            optimize_video, probe, video_info)


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
PROGRAM_ALIASES = {**PROGRAM_ALBUMS, "LESMILLSCORE": "CORE", "LESMILLSGRIT": "GRIT", "LESMILLSTONE": "TONE",
                   "莱美核心": "CORE", "莱美核心训练": "CORE"}
VIDEO_PROFILES = {"ipad": (1080, 1800), "compact": (720, 950)}


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
    if "releaseDate" in value:
        text = value["releaseDate"]
        match = re.fullmatch(r"([0-9]{4})(?:Q[1-4]|-([0-9]{2})(?:-([0-9]{2}))?)?", text)
        require(match, "Use YYYY, YYYYQn, YYYY-MM or YYYY-MM-DD for the release date")
        date(int(match[1]), int(match[2] or 1), int(match[3] or 1))
    return program, routine, revision


def author_identity(project, routine):
    raw = project.strip().upper()
    key = re.sub(r"[ ._-]", "", raw)
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
            return duration, f"The album tag points to {source[0]} {source[1]}."
    return duration, None


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
    program, routine = author_identity(args.program, args.routine)
    value = {"program": program, "routine": routine, "revision": args.revision, "music": []}
    if args.credit:
        value["credit"] = args.credit
    if args.release_date:
        value["releaseDate"] = args.release_date
    read_identity(value)
    recognized = {}
    assets = {"video": [], "pdf": [], "cover": []}
    review = []
    warnings = []
    for file in sorted(root.rglob("*")):
        if not file.is_file() or file.is_symlink() or any(part.startswith(".") for part in file.relative_to(root).parts):
            continue
        suffix = extension(file)
        if suffix in AUDIO:
            match = re.match(r"^(?:(ALTERNATIVE|ALT|BNS|BONUS|替换曲|替换|备选|A|B)[ ._-]*)?T?(\d{1,3})([A-Z]?)(?:[ ._-]+(.*)|$)",
                             file.stem, re.I)
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
            if value["program"] == "BC" and letter:
                review.append(str(file))
                continue
            try:
                duration, conflict = labeled_album(file, value["program"], value["routine"])
            except (ValueError, TypeError, KeyError):
                duration, conflict = 0, "Audio tracks or duration could not be verified."
            if conflict:
                review.append(str(file))
                warnings.append({"file": file.name, "section": section, "reason": conflict})
                continue
            item = {"section": section, "variant": variant, "file": str(file), "title": title or section}
            recognized.setdefault((section, variant), []).append((item, duration))
        elif suffix in VIDEO:
            assets["video"].append(str(file))
        elif suffix == "pdf":
            assets["pdf"].append(str(file))
        elif suffix in PICTURE and re.search("cover|artwork|封面", file.stem, re.I):
            assets["cover"].append(str(file))
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
        cover = args.cover.resolve(strict=True)
        require(cover.is_file() and extension(cover) in PICTURE, "Choose a JPEG or PNG cover")
        value["cover"] = str(cover)
    value["reviewSources"] = review
    value["musicWarnings"] = warnings
    write_json(args.output, value)
    print(json.dumps({"musicProposed": len(value["music"]), "reviewSources": len(review),
                      "musicWarnings": len(warnings)}, ensure_ascii=False))


def analyze(args):
    config = read_json(args.plan)
    read_identity(config)
    songs = config["music"]
    require(1 <= len(songs) <= 128, "Choose the music files in the plan")
    audio = []
    for row in songs:
        require(re.fullmatch(r"T[1-9][0-9]{0,2}[A-Z]?", row["section"]) is not None
                and row["variant"] in VARIANTS, "Invalid music section or variant")
        path = Path(row["file"]).resolve(strict=True)
        require(extension(path) in AUDIO, f"Unsupported audio: {path.name}")
        duration, _ = probe(path, "audio")
        audio.append({**row, "file": path, "duration": duration, "source": identity(path)})
    review = {"plan": config, "tracks": [{"source": row["source"], "duration": row["duration"]} for row in audio]}
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
            try:
                audio_duration = float(video_audio.get("duration", duration))
            except (TypeError, ValueError):
                audio_duration = duration
            chapters, warnings = locate_chapters(video, audio, duration, audio_duration)
            review["videoWarnings"] = warnings
            for record, chapter in zip(review["tracks"], chapters):
                record["chapter"] = chapter
    if config.get("cover"):
        review["cover"] = identity(Path(config["cover"]).resolve(strict=True))
    write_json(args.output, review)
    print(json.dumps({"music": len(audio), "chaptersProposed": sum(bool(x.get("chapter")) for x in review["tracks"]),
                      "cueRowsProposed": sum(len(x.get("cues", {}).get("rows", [])) for x in review["tracks"])}))


def same_source(value, expected):
    path = Path(value).resolve(strict=True)
    require(identity(path) == expected, f"Source changed since review: {path.name}")
    return path


def validate_manifest(value):
    read_identity(value)
    songs = value["music"]
    require(value["schemaVersion"] == 1 and 1 <= len(songs) <= 128, "Unsupported package manifest")
    if "credit" in value:
        credit = value["credit"]
        require(0 < len(credit.encode("utf-8")) <= 128 and all(ord(c) >= 32 and not 127 <= ord(c) <= 159 for c in credit),
                "Invalid contributor credit")
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
        require(1 <= pdf["pageCount"] <= 2000, "Invalid PDF page count")
    if cover:
        assets.append(cover)
        allowed.append(({"png", "jpg"}, 8))
    for asset, (extensions, max_mib) in zip(assets, allowed):
        require(asset["ext"] in extensions and 0 < asset["bytes"] <= max_mib * MIB
                and re.fullmatch(r"[a-f0-9]{64}", asset["sha256"]) is not None, "Invalid media entry")
    slots = set()
    for item in songs:
        section = re.fullmatch(r"T([1-9][0-9]{0,2})([A-Z]?)", item["section"])
        require(section and int(section[1]) <= 128 and item["variant"] in VARIANTS
                and 0 < item["duration"] <= 43200, "Invalid music entry")
        key = (item["section"], item["variant"], item.get("catalogTrackID"))
        require(key not in slots, f"Duplicate music slot: {key[0]}")
        slots.add(key)
        chapter = item.get("chapter")
        if chapter:
            require(video and 0 <= chapter["start"] < chapter["end"] <= video["duration"] and
                    chapter["evidence"] in {"audioMatched", "visuallyConfirmed", "approximate"},
                    "Invalid chapter timing")
            if chapter["evidence"] == "audioMatched":
                require(abs(chapter["end"] - chapter["start"] - item["duration"]) <= 0.25,
                        "Audio match is not a complete unit-rate video window")
        if "pdfPage" in item:
            page = item["pdfPage"]
            require(pdf and type(page) is int and 1 <= page <= pdf["pageCount"],
                    f"Invalid original PDF opening page for {item['section']}")
        rows = item.get("cues", [])
        require(not rows or (pdf and len(rows) <= 4096), "Cues require a bounded source PDF")
        previous = -1
        for cue in rows:
            require(previous <= cue["time"] <= item["duration"] and cue["time"] >= 0 and
                    type(cue["page"]) is int and 1 <= cue["page"] <= pdf["pageCount"] and
                    cue["action"].strip(), f"Invalid cue for {item['section']}")
            previous = cue["time"]
            for key in ("action", "music", "counts", "repetitions", "coaching"):
                if key in cue:
                    require(len(cue[key].encode("utf-8")) <= 4096, f"Long cue field for {item['section']}")
            if level := cue.get("level"):
                require(all(0 <= level[channel] <= 1 for channel in ("red", "green", "blue")), "Invalid PDF level color")
    return assets


def package(args):
    review = read_json(args.review)
    config = review["plan"]
    program, routine, revision = read_identity(config)
    require(-24 <= args.lufs <= -12, "Select a loudness target between -24 and -12 LUFS")
    require(len(config["music"]) == len(review["tracks"]), "Review does not cover every music file")
    if args.video_quality == "original":
        require(args.video_max_height is None and args.video_bitrate_kbps is None and args.video_codec is None,
                "Choose ipad or compact before setting video encoding options")
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
        for index, (song, findings) in enumerate(zip(config["music"], review["tracks"])):
            path = same_source(song["file"], findings["source"])
            if not args.keep_music:
                path, levels = balance_audio(path, work / f"audio-{index}.m4a", args.lufs)
                loudness.append({"section": song["section"], **levels})
            duration, _ = probe(path, "audio")
            row = {key: song[key] for key in ("section", "variant", "catalogTrackID", "title", "pdfPage") if key in song}
            row.update(asset=identity(path), duration=round(duration, 6))
            if (chapter := findings.get("chapter")) and chapter.get("approved") is True:
                row["chapter"] = {key: chapter[key] for key in ("start", "end", "evidence")}
                if chapter["evidence"] == "audioMatched":
                    row["chapter"]["end"] = round(chapter["start"] + duration, 6)
            if (cues := findings.get("cues")) and cues.get("approved") is True and cues["rows"]:
                row["cues"] = cues["rows"]
            files.append(path)
            manifest["music"].append(row)
        if config.get("video"):
            path = same_source(config["video"], review["video"]["source"])
            original_picture = video_info(path)
            if args.balance_video:
                path, levels = balance_audio(path, work / "video.mp4", args.lufs, video=True)
                loudness.append({"section": "video", **levels})
            if args.video_quality != "original":
                height, bitrate = VIDEO_PROFILES[args.video_quality]
                path, encoded_picture = optimize_video(path, work / "compact.mp4",
                    args.video_max_height or height, args.video_bitrate_kbps or bitrate, args.video_codec or "hevc")
            else:
                encoded_picture = video_info(path)
            video_quality = {"preset": args.video_quality, "source": original_picture, "output": encoded_picture}
            duration, _ = probe(path, "video")
            manifest["video"] = {"asset": identity(path), "duration": round(duration, 6)}
            files.append(path)
        if config.get("pdf"):
            path = same_source(config["pdf"], review["pdf"]["source"])
            manifest["pdf"] = {"asset": identity(path), "pageCount": review["pdf"]["pageCount"]}
            files.append(path)
        if config.get("cover"):
            path = same_source(config["cover"], review["cover"])
            manifest["cover"] = identity(path)
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
    receipt.update(output=str(output.resolve()), loudness=loudness, videoQuality=video_quality)
    print(json.dumps(receipt, ensure_ascii=False, indent=2))


def consume(stream, asset, destination=None):
    remaining = asset["bytes"]
    observed = hashlib.sha256()
    while remaining:
        chunk = stream.read(min(MIB, remaining))
        require(bool(chunk), "Truncated media payload")
        observed.update(chunk)
        if destination is not None:
            destination.write(chunk)
        remaining -= len(chunk)
    require(observed.hexdigest() == asset["sha256"], "Media payload digest mismatch")


def read_manifest(stream):
    require(stream.read(8) == MAGIC, "Invalid package header")
    size = int.from_bytes(stream.read(4), "big")
    require(0 < size <= MAX_MANIFEST, "Invalid manifest length")
    encoded = stream.read(size)
    require(len(encoded) == size, "Truncated manifest")
    return encoded, json.loads(encoded)


def verify(path):
    require(path.stat().st_size <= MAX_PACKAGE, "Package too large")
    with path.open("rb") as stream:
        encoded, manifest = read_manifest(stream)
        for asset in validate_manifest(manifest):
            consume(stream, asset)
        require(not stream.read(1), "Trailing data in package")
    return {"file": path.name, "bytes": path.stat().st_size, "manifestSHA256": hashlib.sha256(encoded).hexdigest(),
            "releaseDate": manifest.get("releaseDate"),
            "music": len(manifest["music"]), "video": bool(manifest.get("video")), "pdf": bool(manifest.get("pdf")),
            "cover": bool(manifest.get("cover")), "chapters": sum("chapter" in m for m in manifest["music"]),
            "pdfOpeningPages": sum("pdfPage" in m for m in manifest["music"]),
            "cueRows": sum(len(m.get("cues", [])) for m in manifest["music"])}


def doctor():
    import numpy
    import pdfplumber

    for name in ("ffmpeg", "ffprobe"):
        require(shutil.which(name), f"Install {name} and add it to the agent terminal's PATH")
    alignment_module()
    print(json.dumps({"python": sys.version.split()[0], "numpy": numpy.__version__,
                      "pdfplumber": pdfplumber.__version__,
                      "ocr": all(shutil.which(name) for name in ("pdftoppm", "tesseract"))}))


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
    p.add_argument("--cover", type=Path, help="A verified local JPEG or PNG cover")
    p.add_argument("--output", required=True, type=Path)
    p = commands.add_parser("analyze")
    p.add_argument("plan", type=Path)
    p.add_argument("--output", required=True, type=Path)
    p.add_argument("--ocr", action="store_true")
    p.add_argument("--skip-alignment", action="store_true")
    p = commands.add_parser("build")
    p.add_argument("review", type=Path)
    p.add_argument("--output-dir", required=True, type=Path)
    p.add_argument("--lufs", type=float, default=-16.0)
    p.add_argument("--keep-music", action="store_true")
    p.add_argument("--balance-video", action="store_true")
    p.add_argument("--video-quality", choices=("original", "ipad", "compact"), default="original")
    p.add_argument("--video-max-height", type=int, help="Override the selected preset's height limit")
    p.add_argument("--video-bitrate-kbps", type=int, help="Override the selected preset's video bitrate")
    p.add_argument("--video-codec", choices=("hevc", "h264"), help="HEVC by default for ipad and compact")
    p = commands.add_parser("verify")
    p.add_argument("package", type=Path)
    args = parser.parse_args()
    try:
        if args.command == "doctor":
            doctor()
        elif args.command == "verify":
            print(json.dumps(verify(args.package), ensure_ascii=False, indent=2))
        else:
            {"plan": plan, "analyze": analyze, "build": package}[args.command](args)
    except (ValueError, KeyError, TypeError, OSError, ImportError, subprocess.SubprocessError) as error:
        parser.exit(1, f"{error}\n")


if __name__ == "__main__":
    main()
