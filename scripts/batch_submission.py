"""Seal a batch of checked SSplayer packages for handoff."""

from argparse import Namespace
from collections import Counter
from contextlib import redirect_stdout
from datetime import datetime
from io import StringIO
from pathlib import Path
from uuid import uuid4
import hashlib
import json
import re
import subprocess
import sys
import tempfile

from ssroutine import MAX_PACKAGE, author_identity, digest, read_json, read_manifest, require, validate_manifest, write_json
from media_analysis import unresolved_repeat_times
from delivery_policy import assess_delivery, blocker_lines


ITEM_ERRORS = (ValueError, KeyError, TypeError, OSError, subprocess.SubprocessError)


def delivery_batch_id(args):
    batch = args.batch_id or f"SSplayer-{datetime.now():%Y%m%d}-{uuid4().hex[:8]}"
    require(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]{0,63}", batch), "Invalid batch ID")
    require(not (args.output_dir / batch).exists(), f"Batch already exists: {batch}")
    return batch


def subtitle_observations(args):
    path = getattr(args, "subtitle_observations", None)
    if path is None:
        return {}
    require(getattr(args, "screen_subtitles", None) is None,
            "Choose either a per-package subtitle record or --screen-subtitles")
    rows = read_json(path)
    require(isinstance(rows, dict) and rows, "Expected a nonempty subtitle observation object")
    for name, entry in rows.items():
        require(Path(name).name == name and name.endswith(".ssroutine") and isinstance(entry, dict)
                and set(entry) == {"sha256", "status"}
                and isinstance(entry["sha256"], str) and re.fullmatch("[0-9a-f]{64}", entry["sha256"])
                and entry["status"] in ("present", "absent"),
                "Bind each subtitle observation to the exact package name and SHA-256")
    return rows


def record_block(blocked, source, stage, error, keep_going):
    if not keep_going:
        raise ValueError(f"{source.name} · {stage}\n{error}") from error
    blocked.append({"source": str(source.resolve()), "stage": stage, "reason": str(error)})
    print(f"{source.name} · {stage} · 暂缓交付\n{error}", file=sys.stderr, flush=True)


def package_header(source):
    require(source.is_file() and not source.is_symlink(), f"Select a regular package file: {source.name}")
    require(source.stat().st_size <= MAX_PACKAGE, f"Package exceeds size limit: {source.name}")
    with source.open("rb") as stream:
        encoded, manifest = read_manifest(stream)
    validate_manifest(manifest)
    for song in manifest["music"]:
        require(not unresolved_repeat_times(song.get("cues") or []),
                f"{source.name} {song['section']} 仍有未展开的重复段")
    program, routine, revision = (manifest[key] for key in ("program", "routine", "revision"))
    require((program, routine) == author_identity(program, routine),
            f"Rebuild with canonical identifiers: {source.name}")
    return f"{program}_{routine}_{revision}.ssroutine", encoded, manifest


def seal_package(source, header, work, font, source_limited, observation, observations):
    from qa_preview import generate

    name, encoded, manifest = header
    program, routine, revision = (manifest[key] for key in ("program", "routine", "revision"))
    stem = Path(name).stem
    package = work / name
    observed = observations.get(name)
    if observed:
        observation = observed["status"]
    print(f"质检 {name}", file=sys.stderr, flush=True)
    with redirect_stdout(StringIO()):
        passed = generate(Namespace(package=source, output_dir=work, credit=None, made_at=None,
                                    font=font, screen_subtitles=observation, source_limited=source_limited),
                          sealed_copy=package, overview_only=True)
    evidence = work / (stem + ".qa")
    report = read_json(evidence / "qa.json")
    require(report["manifestSHA256"] == hashlib.sha256(encoded).hexdigest(),
            f"Package identity changed during review: {source.name}")
    if observed:
        require(report["sha256"] == observed["sha256"],
                f"{name} 的字幕确认对应不同指纹，请复核当前资源包。")
    delivery = assess_delivery(manifest, report, source_limited=source_limited)
    require(passed is True and report["offlineMediaPassed"] is True and delivery["status"] != "blocked",
            f"{name} 未达到投稿交付要求。请修复以下项目后重新质检：\n"
            + "\n".join(blocker_lines(delivery) or report["blockingIssues"]))
    if delivery["status"] == "readyWithLimitations":
        print(f"{name} · 可交付，原始资料限制已列在质检图。", file=sys.stderr, flush=True)
    overview = evidence / "3x3-overview.png"
    require(overview.is_file(), f"Missing 3×3 QA image: {name}")
    return ({
        "program": program, "routine": routine, "revision": revision, "filename": name,
        "bytes": report["bytes"], "sha256": report["sha256"],
        "manifestSHA256": report["manifestSHA256"], "overviewSHA256": digest(overview),
    }, package, overview)


def submit(args, *, blocked=None):
    batch = delivery_batch_id(args)
    require(1 <= len(args.packages) <= 256, "Select between 1 and 256 packages")
    observation = getattr(args, "screen_subtitles", None)
    observations = subtitle_observations(args)
    source_limited = bool(getattr(args, "source_limited", False))
    keep_going = bool(getattr(args, "keep_going", False))
    if blocked is None:
        blocked = []
    require(observation is None or len(args.packages) == 1,
            "An explicit subtitle observation applies to one inspected package")
    root = args.output_dir
    destination = root / batch
    root.mkdir(parents=True, exist_ok=True)
    require(not destination.exists(), f"Batch already exists: {batch}")
    with tempfile.TemporaryDirectory(dir=root, prefix=".ssplayer-batch-") as directory:
        work = Path(directory)
        submission = work / batch
        packages = submission / "套路资源包"
        images = submission / "质检图"
        packages.mkdir(parents=True)
        images.mkdir()
        fingerprints = []
        entries = []
        for source in args.packages:
            try:
                entries.append((source, package_header(source)))
            except ITEM_ERRORS as error:
                record_block(blocked, source, "检查包清单", error, keep_going)
        counts = Counter(header[0] for _, header in entries)
        for source, header in entries:
            if counts[header[0]] != 1:
                record_block(blocked, source, "检查包清单", ValueError(f"同一项目、套路和修订重复：{header[0]}"), keep_going)
                continue
            with tempfile.TemporaryDirectory(dir=work, prefix="item-") as item:
                try:
                    fingerprint, package, overview = seal_package(source, header, Path(item), args.font,
                        source_limited, observation, observations)
                except ITEM_ERRORS as error:
                    record_block(blocked, source, "包内质检", error, keep_going)
                    continue
                package.rename(packages / package.name)
                overview.rename(images / (package.stem + ".png"))
                fingerprints.append(fingerprint)
        require(fingerprints, "没有通过质检的资源包，未生成批次或指纹。")
        fingerprint_name = batch + ".ssfingerprints.json"
        write_json(submission / fingerprint_name, {
            "schemaVersion": 1, "batchID": batch,
            "packages": sorted(fingerprints, key=lambda item: item["filename"]),
        })
        require(not destination.exists(), f"Batch already exists: {batch}")
        submission.rename(destination)
    print(destination.resolve())
    if blocked:
        print(json_summary(destination, fingerprints, blocked), file=sys.stderr)
    return destination


def json_summary(destination, fingerprints, blocked):
    return json.dumps({"status": "partial", "delivery": str(destination.resolve()),
                       "ready": [row["filename"] for row in fingerprints], "blocked": blocked}, ensure_ascii=False)
