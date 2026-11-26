"""Seal a batch of checked SSplayer packages for handoff."""

from argparse import Namespace
from contextlib import redirect_stdout
from datetime import datetime
from io import StringIO
from pathlib import Path
from uuid import uuid4
import hashlib
import re
import sys
import tempfile

from ssroutine import MAX_PACKAGE, author_identity, digest, read_json, read_manifest, require, validate_manifest, write_json
from media_analysis import unresolved_repeat_times
from delivery_policy import assess_delivery, blocker_lines


def delivery_batch_id(args):
    batch = args.batch_id or f"SSplayer-{datetime.now():%Y%m%d}-{uuid4().hex[:8]}"
    require(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]{0,63}", batch), "Invalid batch ID")
    require(not (args.output_dir / batch).exists(), f"Batch already exists: {batch}")
    return batch


def submit(args):
    from qa_preview import generate

    batch = delivery_batch_id(args)
    require(1 <= len(args.packages) <= 256, "Select between 1 and 256 packages")
    observation = getattr(args, "screen_subtitles", None)
    source_limited = bool(getattr(args, "source_limited", False))
    keep_music = bool(getattr(args, "keep_music", False))
    target_lufs = None if keep_music else getattr(args, "target_lufs", None)
    if target_lufs is None and not keep_music:
        target_lufs = getattr(args, "lufs", None)
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
        names = set()
        for source in args.packages:
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
            stem = f"{program}_{routine}_{revision}"
            name = stem + ".ssroutine"
            require(name not in names, f"Duplicate routine revision: {name}")
            names.add(name)
            package = packages / name
            print(f"质检 {name}", file=sys.stderr, flush=True)
            with redirect_stdout(StringIO()):
                passed = generate(Namespace(package=source, output_dir=work, credit=None, made_at=None,
                                            font=args.font, screen_subtitles=observation, target_lufs=target_lufs,
                                            keep_music=keep_music, source_limited=source_limited),
                                  sealed_copy=package, overview_only=True)
            evidence = work / (stem + ".qa")
            report = read_json(evidence / "qa.json")
            require(report["manifestSHA256"] == hashlib.sha256(encoded).hexdigest(),
                    f"Package identity changed during review: {source.name}")
            delivery = assess_delivery(manifest, report, source_limited=source_limited)
            require(passed is True and report["offlineMediaPassed"] is True and delivery["status"] != "blocked",
                    f"{name} 未达到投稿交付要求。请修复以下项目后重新质检：\n"
                    + "\n".join(blocker_lines(delivery) or report["blockingIssues"]))
            if delivery["status"] == "readyWithLimitations":
                print(f"{name} · 可交付，原始资料限制已列在质检图。", file=sys.stderr, flush=True)
            overview = evidence / "3x3-overview.png"
            require(overview.is_file(), f"Missing 3×3 QA image: {name}")
            fingerprint = {
                "program": program, "routine": routine, "revision": revision, "filename": name,
                "bytes": report["bytes"], "sha256": report["sha256"],
                "manifestSHA256": report["manifestSHA256"], "overviewSHA256": digest(overview),
            }
            fingerprints.append(fingerprint)
            overview.rename(images / f"{stem}.png")
        fingerprint_name = batch + ".ssfingerprints.json"
        write_json(submission / fingerprint_name, {
            "schemaVersion": 1, "batchID": batch,
            "packages": sorted(fingerprints, key=lambda item: item["filename"]),
        })
        require(not destination.exists(), f"Batch already exists: {batch}")
        submission.rename(destination)
    print(destination.resolve())
