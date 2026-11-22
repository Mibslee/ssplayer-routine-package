---
name: ssplayer-package-creator
description: "Create and verify local SSplayer routine packages from music, video, choreography PDF and artwork. Resolve replacement recordings, lettered parts, opening pages and timeline clocks before Pro import and class planning."
metadata:
  display-name: ssplayer_package_creator
---

# ssplayer_package_creator

## Collect the two paths

Start by obtaining the original material folder and the output folder. Use any paths the user has already given. Ask for whichever is missing. Read the originals in place. Do analysis and encoding in a local working folder, and place the verified package in the requested output folder. Preserve an existing file if its contents differ.

Identify the stable project code, routine number, and package revision. Preserve only confirmed release-date precision. Check the original source before asking for information that might be in its cover, track list or metadata. Read the [quality checklist](references/quality-check.md) for naming and choosing a cover.

Use the same project code across maintainers. The contributor's project label in SSplayer can differ from that code. Ask which video quality they prefer if there is a large video. The build options support the original, iPad, and compact profiles, with custom height and bitrate.

## Plan and review

Use Python 3.10+, FFmpeg and ffprobe. The complete public Skill includes its alignment helper. For installation and optional scanned-PDF OCR, see [agent setup](references/agent-setup.md).

    python3 scripts/ssroutine.py doctor
    python3 scripts/ssroutine.py plan "/originals" --program TONE --routine 26 --output plan.json
    python3 scripts/ssroutine.py analyze plan.json --output review.json

Read both reviewSources and musicWarnings. Verify the music against the track inventory and printed title, variant and duration. Distinguish replacement and bonus recordings, independent lettered parts such as T3A/T3B, and A/B movement combinations inside one recording. Keep unknown or wrong music out of the published slots.

Read the actual PDF headings, tables, and continuation pages, including CTS, REPS, coaching notes, repeat marks, directions, and printed level colors. Use pdfPages for cue-source physical pages and pdfPage for the approved opening page. Interval lengths are not cue timestamps. Preserve the original PDF and a proven opening page when there is no reliable action clock.

Check video offsets early, midway, and late using the exact recording. Confirm the language edition and full chapter range. Approve audioMatched only for a stable unit-rate match. A visual chapter can use visuallyConfirmed along its own video clock. Keep unverified chapter and cue fields unapproved. Report which information is missing and what remains usable.

## Build and verify

    python3 scripts/ssroutine.py build review.json --output-dir "/deliverables"
    python3 scripts/ssroutine.py verify "/deliverables/TONE_26_1.ssroutine"

Follow the [quality checklist](references/quality-check.md) to check playback, loudness, true peak, hashes, and the selected opening pages. With a compatible Pro App, exercise import, media and pages, class selection, save and reopen, and duplicate import. Report the checks that actually ran and the missing or unresolved media. Give the user the verified PROJECT_ROUTINE_REVISION.ssroutine.

For re-encoded video, verify the frame timestamps and retained audio before accepting the file, then remeasure all declared audio matches in the offline report. Show the resulting codec, size, resolution and measured bitrate. Raise the package revision when changing an already shared video.

For review before Pro access, run `scripts/qa_preview.py PACKAGE --output-dir OUTPUT`. Use the user-provided credit. Deliver the nine images, 3×3 overview, and visual report.html. The report expands all measured tracks and shows gaps first. It remeasures declared audio matches with the packaged media. Record App acceptance separately.

Use the [format reference](references/format.md) for manifest editing or compatibility errors.
