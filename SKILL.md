---
name: ssplayer-package-creator
description: "Create and verify local SSplayer routine packages from music, video, choreography PDF and artwork. Resolve replacement recordings, lettered parts, opening pages and complete action timelines before Pro import and class planning."
metadata:
  display-name: ssplayer_package_creator
---

# ssplayer_package_creator

## Collect the two paths

Start by obtaining the original material folder and the output folder. Use any paths the user has already given. Ask for whichever is missing. Read the originals in place. Do analysis and encoding in a local working folder, and place the verified package in the requested output folder. Preserve an existing file if its contents differ.

Identify the stable project code, routine number, and package revision. Preserve only confirmed release-date precision. Check the original source before asking for information that might be in its cover, track list or metadata. Read the [quality checklist](references/quality-check.md) for naming and choosing a cover.

For multiple routines, present one table with source subfolders, proposed project codes, routine numbers, revisions, date evidence and cover choices. Confirm shared defaults for attribution and video quality once. Let the creator set exceptions in the table. Build and seal only the selected, reviewed groups.

Use the same stable project code across maintainers. For example, 莱美核心 and Les Mills CORE both produce `CORE_63_1.ssroutine` for release 63, revision 1. Normalize numeric release codes before building. Use a higher integer revision when correcting something already shared. Build with the default efficient profile unless the creator selects original, iPad or compact. For a specific device or size limit, use the height and bitrate options.

## Plan and review

Use Python 3.10+, FFmpeg and ffprobe. The complete public Skill includes its alignment helper. For installation and optional scanned-PDF OCR, see [agent setup](references/agent-setup.md).

    python3 scripts/ssroutine.py doctor
    python3 scripts/ssroutine.py plan "/originals" --program TONE --routine 26 --output plan.json

Show the actual images from `coverCandidates` to the creator with their sources. Ask them to confirm the artwork for this release or choose an empty cover. Use `select-cover` to record the selected file or omission, then analyze the resulting plan. A creator-approved explicit file can also be passed to `plan --cover`. Keep choreography pages with the original PDF. See the [cover selection instructions](references/quality-check.md#封面).

    python3 scripts/ssroutine.py analyze confirmed-plan.json --output review.json

Read both reviewSources and musicWarnings. Verify the music against the track inventory and printed title, variant and duration. Distinguish replacement and bonus recordings, independent lettered parts such as T3A/T3B, and A/B movement combinations inside one recording. Keep unknown or wrong music out of the published slots.

The planner recognizes names such as `T01 - Steady Steps.wav` and `TONE 26 T01 - Steady Steps.wav` for the selected release. When a verified source appears in `reviewSources`, record its original path and confirmed `section`, `variant` and `title` in `plan.music` before analysis. Use the same shape as the proposed entries and keep the originals in place.

Read the actual PDF headings, tables, and continuation pages, including CTS, REPS, coaching notes, repeat marks, directions, and printed level colors. Use pdfPages for cue-source physical pages and pdfPage for the approved opening page. Interval lengths are not cue timestamps. Preserve the original PDF and a proven opening page when there is no reliable action clock.

Write the finished action timeline into each recording's reviewed cue rows. Expand structural repeats before approving the track. Follow the [timeline authoring checks](references/quality-check.md#把完整动作时间线写入包) for source boundaries, clocks and side changes. A finished package supplies the recording identity, variant, split section, video timing, original PDF page and available cue rows. Missing evidence stays absent and the QA cards show what the coach can use.

Check video offsets early, midway, and late using the exact recording. Confirm the language edition and full chapter range. Approve audioMatched only for a stable unit-rate match. A visual chapter can use visuallyConfirmed along its own video clock. Keep unverified chapter and cue fields unapproved. Report which information is missing and what remains usable.

## Build and hand off a batch

    python3 scripts/ssroutine.py build review.json --output-dir "/local-work/output"
    python3 scripts/ssroutine.py submit "/local-work/output/TONE_26_1.ssroutine" "/local-work/output/CORE_63_1.ssroutine" --output-dir "/chosen-output"

Rerun analysis after changing a section, variant, catalogue slot, or PDF cue-page selection. The review can still approve a source-backed opening page, amend display text or record what is missing.

Use the [quality checklist](references/quality-check.md) to review what the package can do. Resolve the strict report's media failures before delivery. Leave missing or unverified items clearly identified. With a compatible Pro App, select the package through an actual import entry, verify its destination and media, then save and reopen a class using the imported tracks.

The efficient profile tests a quality-driven HEVC encode and retains the input if size savings, sample picture quality, time stamps or stream verification fail. Check the build receipt for the result. The batch command remeasures declared audio matches and renders each package's QA cards. Review motion, text and picture subtitles at the checked times before uploading.

Use the user-provided credit. Submit the generated batch folder through the creator's chosen cloud drive. It contains the `套路资源包` folder, the `质检图` folder with nine images and a 3×3 overview per package, and one `.ssfingerprints.json`. Keep the planning files, encoding intermediates and standalone HTML report in local work. Offline QA and App acceptance are recorded separately.

The first card summarizes the routine, cover, package size and the finished media's specifications. Report embedded subtitle tracks from the packaged video. Use visual review to confirm subtitles printed into the picture.

For a correction to a shared package, extract its manifest, amend the reviewed metadata, and run `revise` with a higher revision. The command reuses the exact audio, video and PDF. It keeps the previous cover by default. Show the creator and get confirmation before passing a replacement with `--cover` or omitting it with `--without-cover`. Run `submit` again and send the new batch for signature. See the [format reference](references/format.md) for the commands and fields.
