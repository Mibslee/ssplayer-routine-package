---
name: ssplayer-routine-package
description: "Prepare a local SSplayer .ssroutine package from coach-provided BC, BJ, or other routine media. Use for identifying music slots, matching video chapters, reading choreography PDFs, balancing loudness, packaging, or validating an SSplayer contribution."
---

# Make an SSplayer routine package

Use the user's local music, video, original choreography PDF and optional cover. Produce one portable package named PROJECT_ROUTINE_REVISION.ssroutine. Ask about ownership only when the user's intended distribution requires a rights decision. Keep original files intact and process them locally.

Use [scripts/ssroutine.py](scripts/ssroutine.py) with Python 3.10+, FFmpeg and ffprobe. The Python dependencies are in [requirements.txt](requirements.txt). Optional scanned-page analysis uses Poppler's pdftoppm and Tesseract.

## Identify the routine

Run the plan command from this skill directory, using an output path outside the original media folder.

    python3 scripts/ssroutine.py plan "/path/to/routine" --program BC --routine 108 --revision 1 --output plan.json

Review every selected file and each entry in reviewSources. Correct course identity, primary/alternate assignments, and duplicate source versions against the actual files and source track list. For a catalogue slot with multiple candidates, supply the matching catalogTrackID. Use T3 for a complete section or T3A for an independently identified part. Preserve a cover if available. Optional credit is the contributor's chosen public name. Use a new revision after changing a delivered package.

## Analyze and review

    python3 scripts/ssroutine.py analyze plan.json --output review.json

- Check video alignment early, midway and late in each independently matched track. Compare the actual music and video at the proposed offset. Verify the selected audio language and that the entire window fits. Approve audioMatched only for a consistent unit-rate match. A verified visual chapter can use visuallyConfirmed. Leave ambiguous or unmatched chapters out of the published timing.
- If the video has narration only, analysis can run with --skip-alignment. A verified chapter may be entered manually in the review using start, end, evidence and approved. Keep timing unresolved where the source is ambiguous.
- Read action rows against the physical source PDF pages. The timestamp is relative to that music section. Check alternate and split-section headings, continuation lines, counts, repeats, coaching notes and literal LEVEL colours. Preserve a printed repeat mark as Repeat if the glyph is absent from extraction. Add pdfPages to a plan entry when the page owner needs an explicit choice. For image-only pages, rerun with --ocr into a fresh review and compare against the rendered PDF. Keep the source PDF even if the action rows remain unresolved.
- Set approved to true only on the reviewed chapter and cue collection for that recording. Keep exact source identities in the review. If a file changes, rerun analysis. Report unresolved sections with their reason.

## Build and check

    python3 scripts/ssroutine.py build review.json --output-dir "/path/to/output"
    python3 scripts/ssroutine.py verify "/path/to/output/BC_108_1.ssroutine"

Music is measured for integrated LUFS and true peak. The default target is −16 LUFS, with headroom and constant gain. When necessary, write a separate lossless audio copy and check its measured duration and loudness. Use --balance-video when the one-stream video audio also needs balancing. Verify the picture bitstream, picture timing and audio start. The tool hashes the finished bytes and reads the whole package back before delivery.

In a compatible SSplayer build with Pro active, import the single file from Files or the app's local import picker. Check the project, routine, track order, cover, original PDF, music playback, video seek and action rows, then reopen the app and reimport the package to check persistence and duplicates. Report the results that were actually verified. Give the user the package and the concise counts from the verification output.

Use [the format reference](references/format.md) when inspecting a package contract, editing a plan or diagnosing an import refusal.
