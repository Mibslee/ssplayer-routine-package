---
name: ssplayer-package-creator
description: "Create, repair, or verify local SSplayer routine packages from music, video, choreography PDFs and cover art. Interpret the score, bind recordings to cues and class formats, and seal a checked contribution batch."
metadata:
  display-name: ssplayer_package_creator
  version: "1.7.10"
---

# SSplayer package creator

Use the source material to decide what a coach should hear, see, and select in SSplayer. The model owns that interpretation. The scripts extract candidates and verify the selected files, times, pages, playable formats, and package bytes.

## Establish the batch

- Use the source and destination authorized for this project. Work in local scratch space. Use `--workspace` to deliver under the actual project's `交付` folder, or `--output-dir` for an explicitly selected destination. Show the resolved destination.
- Run `doctor` and `plan`. Compare the candidate recordings with the score, track list, and video. Settle the program, edition, display names, and any real ambiguity in `reviewSources` or `musicWarnings`.
- Present unresolved cover, attribution, media and destination choices together. Apply choices already given. Check all distinct embedded covers and use one confirmed exact-edition cover throughout the routine. List “音频原样，封面标签统一” and the default H.265 (hvc1). Use a saved byline when appropriate and save a default on request. Use reliable edition-specific sources for any missing release date and keep their precision.
- Run `confirm-plan` and `analyze`. For an interrupted batch, start at its latest confirmed plan, reviewed files, and verified packages. See [setup and resuming a batch](references/agent-setup.md#批次接手与中断恢复) and [cover and identity details](references/quality-check.md).

## Interpret the score and the recordings

Read the relevant original pages with the recording and course context in mind. Use `inspect-score PDF --pages 4,6-8 --output-dir SCRATCH` to see the physical page images and positioned text when layout matters. Check the title, neighboring pages and the exact recording as needed. Treat PDF text, OCR and automated extraction as observations. Treat text found in the source as course material.

- **Identify the recording.** A printed A/B can be a playable recording, a course choice or a movement sequence. Check the music list, song, duration, and score relationship. A source may explicitly assign one choreography to a named substitute or a portion of it to a short edit. Verify that edit's own clock. Keep the PDF opening page distinct from the pages used for cues.
- **Read the page like a coach.** Identify the role of each column and number before turning it into data. Absolute starts, elapsed durations, counts, repeats, and simultaneous exercises have different meanings. Use the visible group boundaries and continuation pages. Expand supported repeated sequences, preserving direction, counts, cues, and color. Keep an unclocked group together at its known start. Write individual action-to-count links only where the layout supports them.
- **Verify the chapter and the class.** Check the selected recording at independent early, middle, and late points. For doubtful chapters, inspect the visible chapter title and clock, including short or bonus footage after the main class. Label audio matching and visual clock evidence according to what was established. Build each printed format from its actual ordered slots, choices, repeats, and required editions. Verify that the packaged recordings fill it.
- **Resolve disagreements from the source.** Correct the smallest part of the draft that is wrong. When the model can verify a printed role that the parser cannot read, record the page, visible label, and recording link in `sectionFunctionReview`. Keep remaining uncertainty specific to the claim it affects. Ask only when a missing decision or genuinely conflicting source would change the delivery.

Use [the score review guide](references/quality-check.md#模型如何复核原谱) for unfamiliar layouts, [the clock guide](references/video-clock.md) for offset and drift, and the course notes for [BODYATTACK](references/quality-check.md#bodyattack-替换曲express-与课型), [BODYPUMP](references/quality-check.md#bodypump-block-与重复段), or [duration tables](references/quality-check.md#分段时长表与曲内时钟) when relevant.

## Verify, build and deliver

- Run `check-review` before encoding. Use `--measure-video` when alignment needs a fresh measurement. Use `--source-limited` for a confirmed source limitation. Resolve contradictory recording, action, and class evidence before delivery. Continue independent routines while a specific one needs work.
- Use `finish` for reviewed material and `--keep-going` for a batch. Preserve the audio packets, original dynamics, and selected video timing. Cover tag repairs use the confirmed artwork and leave the audio packets intact. Validate H.265, frame timing, audio/subtitle streams, and sampled quality. Apply an explicitly requested alternate video preset when relevant.
- Open `qa_preview.py PACKAGE --strict` on blocked packages and repair the failing layer. Use `submit` or `finish --include-package` to reuse verified packages. Check existing encoders before restarting. See [local repairs](references/agent-setup.md#失败恢复与局部修订) and [subtitle evidence](references/agent-setup.md#字幕与交付范围).
- Deliver one project folder with `套路资源包`, one 3×3 PNG per routine under `质检图`, and a single `<project>.ssfingerprints.json` covering the exact set. Reseal the complete selected set when updating a project. Read back the delivered checksums. Report which routines and checks passed, the material limitations, and any separate App verification.
- Keep original materials, draft reviews, raw evidence and working logs local. Share only the specified deliverables and destinations authorized by the user. Publisher approval and signing are separate from local package verification.
