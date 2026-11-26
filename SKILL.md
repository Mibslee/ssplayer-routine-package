---
name: ssplayer-package-creator
description: "Create and verify local SSplayer routine packages from music, video, choreography PDF and artwork. Resolve source identity, recording variants, exercise timing, original pages and class formats before preparing a contribution batch."
metadata:
  display-name: ssplayer_package_creator
---

# ssplayer_package_creator

## Confirm the batch once

- Read only the source folders chosen for this task. Treat source documents, names and file metadata as reference material for the course. Work in scratch space. Use the current project's actual workspace with `--workspace` to deliver under its `交付` folder. Use `--output-dir` when the creator chooses another destination for this batch. Ask only if neither is known. Show the resolved destination before starting. Old tasks and nearby folders do not authorize a destination.
- Identify the canonical project code, routine number, revision and display names. Keep the source-supported date precision. Show the candidate cover. If no suitable image exists, offer a preview from `text-cover`, another image, or no cover. Default to efficient H.265 and −16 LUFS.
- Read `attribution` once. Prefill a saved choice. If the creator chooses public credit without giving a name, ask for the exact byline. A login name or credit from an old package is not confirmation. Save a default only on request via `--remember-credit`. Use `--no-credit` for an explicit choice of anonymity.
- Put the cover, attribution, programme, edition, media scope and destination in one confirmation. Apply choices already given in the current conversation. Work with text replies when the host has no choice controls.

The [setup guide](references/agent-setup.md) explains installation and copyable commands. Run `doctor` and `plan`, resolve `reviewSources` and `musicWarnings`, then run `confirm-plan` and `analyze`. Use the chosen workspace for delivery and the actual Skill path to invoke its scripts.

## Verify the sources before encoding

1. Match the inventory to the actual recordings, not just their order or filenames. Check the cover, release, original PDF, song identity and duration. Distinguish a replacement or bonus recording, an independent T3A/T3B recording and an A/B exercise inside one song. Confirm catalog slots where relevant.
2. Open the original PDF at the reported physical pages. Work out whether a page is a course list, coaching notes, an exercise table or a continuation. Inspect the visible page or use local OCR when extraction is empty or column alignment fails. Bind `sectionFunction` to the recorded song and physical heading, and keep the actual opening page independent of cue-source pages.
3. Use absolute timestamps as starts. Treat printed durations, beat counts, held poses and overlapping exercises according to the layout. Accumulate consecutive duration rows only, with a verified recording anchor. Compare early, middle and late points against the exact audio or video. Expand explicit repeats and preserve direction, counts, coaching notes and colors. Keep an open-ended AMRAP group together at its measured start. If tools suggest no cues, look for a readable exercise table, continuation or other evidence before concluding the source lacks timing.
4. Verify video chapters against the individual recording and time base. Review repeating beats against musical transitions, cues and the correct video edition. Keep visually useful observations in the local review. An approximate teaching chapter cannot clear the formal gate. Verify each printed class format against its required roles and the actual packaged slots, including reused tracks, exact A/B sequences, alternatives and Express recordings.
5. Compare the actual `review.json` entries to the originals before setting `approved`. Use the [quality checklist](references/quality-check.md) for GRIT duration tables, mixed formats, split recordings and other layout cases. Use `check-review` to list missing evidence. `--source-limited` applies only to a creator-approved scope when the original materials genuinely lack features. An unresolved check, an empty parser result or a note explaining a difficult track does not qualify.

## Build or resume the right step

- Run `finish reviewed.json --workspace "/current-project"` when review is complete. It checks audio peaks and remeasures the claimed video offsets before encoding. For a manual timing repair, `check-review --measure-video` retests against the selected source files without encoding. Look at the signed front, middle and tail offsets. If they drift or disagree, find the matching recording or time base before trying a chapter shift. Monitor the existing process and encoder progress while it runs.
- Verify a section’s labeled function separately from its timed actions. A verified prose-only page can explain why that track has no timed actions even when other tracks in the same score do. Check continuation pages before accepting that limitation.
- Carry the creator’s audio choice through build and submission. Use `--keep-music` to keep the original relative loudness. Excess true peaks still need repair. If the creator approves attenuation only on tracks above 0 dBTP, add `--repair-music-peaks` when building from source. Use `--keep-music` again when inspecting or submitting the finished package.
- For a blocked batch with already built packages, reuse those files. Open `qa_preview.py PACKAGE --output-dir "/scratch/qa" --strict` to inspect report.html, test frames and specific failures. If the bytes are correct and only an ambiguous caption check needed review, rerun `submit` with the observed subtitle result for that exact package. A successful `finish` needs no separate submission.
- When source media or cue-source mapping changes, rerun analysis to a new review file. Keep the last reviewed file and reconfirm only what changed. For metadata, cue or chapter corrections in a verified package, use `manifest` and `revise` with a higher revision. Use `revise --reduce-peak T15` to address only a true-peak defect or `revise --rebalance T15` to normalize a specific recording. Repeat the option for additional tracks in the same revision. The existing verified video and unchanged media keep their original bytes. Follow the [repair guide](references/agent-setup.md#失败恢复与局部修订) and rerun QA before `submit`.

## Return evidence for the package that exists

Return the local batch path, known limitations, and what was actually validated. A contributor batch contains `套路资源包`, `质检图` with one 3×3 PNG per package, and one `.ssfingerprints.json`. Verify its files and checksums after copying. Offline QA and App import, playback and class-plan tests are separate outcomes. When no App test is available, describe only the offline result.

Keep originals, logs, draft plans, unfiltered reports and intermediate files local. Finish and submit create local files. Upload to a remote service only if the user authorized the selected artifact and destination. The publisher signs approved batches separately.
