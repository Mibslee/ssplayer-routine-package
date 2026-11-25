---
name: ssplayer-package-creator
description: "Create and verify local SSplayer routine packages from music, video, choreography PDF and artwork. Resolve replacement recordings, lettered parts, opening pages, source-verified section functions and complete action timelines before Pro import and class planning."
metadata:
  display-name: ssplayer_package_creator
---

# ssplayer_package_creator

## Collect the two paths

Start by obtaining the original material folder and the output folder. Use any paths the user has already given. Ask for whichever is missing. Read the originals in place. Do analysis and encoding in a local working folder, and place the verified package in the requested output folder. Preserve an existing file if its contents differ.

Identify the stable project code, routine number and package revision from the originals. Propose a project display name and a routine display name, such as 莱美核心 and 核心训练第 63 期. Keep the names distinct from the stable codes. Preserve only confirmed release-date precision. Read the [quality checklist](references/quality-check.md) for naming and choosing a cover.

Present one confirmation for the release or batch. List the project name and code, routine name and number, revision, release date, actual cover, attribution and video profile. Let the creator accept the proposal or change just the relevant fields. On a host with choice controls, offer **确认开始**, **调整选项** and **输入要求**. In a text-only chat, accept the same intent in a sentence. Reuse values the creator has already confirmed. For a batch, show shared defaults once and list release-specific changes.

Run `ssroutine.py attribution` to see whether a local choice exists. If none exists, offer a public attribution or no attribution in the same confirmation. Let the user save that choice as the local default. With a saved choice, prefill it on the confirmation and start when the creator accepts; do not ask them to type it again. Allow a change for this batch, updating the default, or omitting attribution. If the user supplied attribution for this batch, honor it. Remember only when they choose to apply it to future batches. Use efficient H.265 and −16 LUFS by default.

Use the same stable project code across maintainers. For example, 莱美核心 and Les Mills CORE both produce `CORE_63_1.ssroutine` for release 63, revision 1. Normalize numeric release codes before building. Use a higher integer revision when correcting something already shared. Build with the default efficient profile unless the creator selects original, iPad or compact. For a specific device or size limit, use the height and bitrate options.

## Plan and review

Use Python 3.10+, FFmpeg and ffprobe. The complete public Skill includes its alignment helper. For installation and optional scanned-PDF OCR, see [agent setup](references/agent-setup.md).

    python3 scripts/ssroutine.py doctor
    python3 scripts/ssroutine.py plan "/originals" --program TONE --routine 26 --output plan.json

Show the actual images from `coverCandidates` and use the chosen image or omission in the same confirmation. Run `confirm-plan` once per release with the accepted names. It reuses the attribution prefilled in the plan. Pass `--credit` or `--no-credit` to change it or record a first choice, and `--remember-credit` only when the creator requested a default. Apply a shared batch choice to all the plans in that batch. The confirmed credit goes into the package and its batch QA. Use `--cover` with the accepted image, or `--without-cover` when the creator accepts omitting it. Keep choreography pages with the original PDF. See the [confirmation and cover commands](references/agent-setup.md#命令行示例).

    python3 scripts/ssroutine.py confirm-plan plan.json --project-name "TONE" --routine-name "TONE 第 26 期" --without-cover --no-credit --output confirmed-plan.json
    python3 scripts/ssroutine.py analyze confirmed-plan.json --output review.json

Read both reviewSources and musicWarnings. Verify the music against the track inventory and printed title, variant and duration. Distinguish replacement and bonus recordings, independent lettered parts such as T3A/T3B, and A/B movement combinations inside one recording. Keep unknown or wrong music out of the published slots.

The planner recognizes names such as `T01 - Steady Steps.wav` and `TONE 26 T01 - Steady Steps.wav` for the selected release. When a verified source appears in `reviewSources`, record its original path and confirmed `section`, `variant` and `title` in `plan.music` before analysis. Use the same shape as the proposed entries and keep the originals in place.

Read the actual PDF headings, tables, and continuation pages, including CTS, REPS, coaching notes, repeat marks, directions, and printed level colors. Use `pdfPages` for cue-source physical pages and `pdfPage` for the approved opening page. Only use printed or independently measured cue clocks. Preserve the original PDF and a proven opening page when there is no reliable action clock. A named section such as Express 05 requires its own recording and verified clock. Check any variants left in `reviewSources`.

Review the section function separately from its song title and cue timing. For each exact recording, including replacements and separately timed A/B parts, verify the numbered function heading on the original choreography page. For example, `01. WARM-UP` supplies `WARM-UP` for T1. Ignore song-title headings on coaching pages and avoid assuming a role from the track number. Record the printed name and physical page in that recording's `review.tracks[].sectionFunction` and set `approved` to true after checking the source. Review strong proposals together with the action rows. Leave uncertain roles absent. Build and QA compare each claimed physical page with the original PDF. An explicit contradiction blocks handoff. Captions that require visual reading appear as manual-review items in QA. See [define section functions](references/quality-check.md#小节功能与排课) for split parts and conflicts.

Write the finished action timeline into each recording's reviewed cue rows. Expand structural repeats before approving the track. Follow the [timeline authoring checks](references/quality-check.md#把完整动作时间线写入包) for source boundaries, clocks and side changes. A finished package supplies the recording identity, variant, split section, video timing, original PDF page and available cue rows. Missing evidence stays absent and the QA cards show what the coach can use. Music-only releases are valid. When a video location or action clock cannot be established, omit that location or track timeline. Reuse any known reason in the plan's `qualityNotes` (`video` or `timeline`).

Use the script's measured offsets for high-confidence audio matches. It checks three distinct parts of the recording and rechecks another fixed set when voice-over or a repeated beat weakens the first set. Review the exceptions against the exact recording and video edition. Use `visuallyConfirmed` for visually verified chapters on the video clock. Leave an unverified chapter unapproved. For action timelines, compare the proposed rows against the chosen PDF, resolve exceptions, then approve finished rows together. Keep a repeat at its printed opening with the actual moves in order; separate clocks need evidence.

## Build and hand off a batch

    python3 scripts/ssroutine.py finish tone-review.json core-review.json --output-dir "/chosen-output"

Use `finish` once per batch with the chosen profile. Leave it running through the reported music, video and QA stages. If a later step fails, use the reported local folder to recover completed releases and continue with `submit`. For different video profiles within a batch, use `build` per reviewed release, then `submit` the selected packages together. Report measured stage times if a run is slow.

Rerun analysis after changing a section, variant, catalogue slot, or PDF cue-page selection. The review can still approve a source-backed opening page, amend display text or record what is missing.

Use the [quality checklist](references/quality-check.md) to review what the package can do. Correct invalid media and claims contradicted by the checks. Accept `conditional` when unprovided optional media or unresolved locations have a visible reason. Review embedded and closed subtitle indicators and sampled picture frames from the actual output. Open `subtitle-samples.jpg` if OCR suggests captions or cannot finish. Confirm picture subtitles after visual review using the [subtitle commands](references/agent-setup.md#字幕与条件通过). Leave missing or unverified items clearly identified. With a compatible Pro App, select the package through an actual import entry, verify its destination and media, then save and reopen a class using the imported tracks.

The efficient profile checks size, sampled picture quality, time stamps and audio/subtitle streams. Encoding profiles require the selected codec, which defaults to HEVC. If the efficient attempt fails and the source has a different codec, adjust the encoding profile with the creator or get their agreement to use original quality. Confirm the packaged codec in the receipt and QA cover. The batch command remeasures declared audio matches and renders each package's QA cards. Review motion, text and picture subtitles at the checked times before uploading.

Use the user-provided credit. Review the phone-style video, action timeline and original PDF previews beside the measured alignment and the package overview. Compare all available tracks with the source and identify unverified timing before handoff. Submit the generated batch folder through the creator's chosen cloud drive. It contains `套路资源包`, `质检图` with one full-resolution 3×3 PNG per package named after the package, and one `.ssfingerprints.json`. Keep planning files, encoding intermediates and standalone HTML reports in local work. Offline QA and App acceptance are recorded separately. The publisher can add this fingerprint file to an existing verified approval in the local Mac signer.

The first card summarizes the routine, cover, package size and the finished media's specifications. Report embedded subtitle tracks from the packaged video. Use visual review to confirm subtitles printed into the picture.

For a correction to a shared package, extract its manifest, amend the reviewed metadata, and run `revise` with a higher revision. The command reuses the exact audio, video and PDF. It keeps the previous cover by default. Show the creator and get confirmation before passing a replacement with `--cover` or omitting it with `--without-cover`. Run `submit` again and send the new batch for signature. See the [format reference](references/format.md) for the commands and fields.
