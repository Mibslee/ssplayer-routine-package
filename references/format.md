# SSplayer portable routine v1

The file name is PROJECT_ROUTINE_REVISION.ssroutine. PROJECT uses 2–16 ASCII uppercase letters/digits and starts with a letter. ROUTINE is a positive number or a short uppercase release code with optional internal hyphens. REVISION is a positive integer. The manifest is the identity and may survive a Files rename.

## Container

Eight ASCII bytes SSRT0001, a four-byte unsigned big-endian JSON length, then the UTF-8 JSON manifest. The rest consists of raw file bytes in the order recorded below. There is no compression, executable metadata, encryption or file-system path in the format. Ordinary archive apps cannot expand it as a ZIP. A program implementing the format can read the payloads. SHA-256 checks content integrity against the manifest, while publisher trust and contribution approval are established outside the format.

Manifest length is at most 4 MiB. The total file is at most 12 GiB. Each asset declares lowercase ext, exact positive bytes, and lowercase whole-file sha256. Files are ordered by the music array, then optional video, PDF, cover and class-templates.json. Missing assets consume no bytes. Approved analysis refers only to the exact media bound into the same manifest.

| Field | Meaning |
| --- | --- |
| schemaVersion | Integer 1 |
| program, routine, revision | Package identity |
| projectName, routineName | Optional display names confirmed by the creator. New custom projects and routines use them when available. |
| credit | Optional contributor credit, at most 128 UTF-8 bytes |
| releaseDate | Optional confirmed YYYY, YYYYQn, YYYY-MM or YYYY-MM-DD |
| music | 1–128 entries containing section, variant, optional catalogTrackID and title, asset, and duration in seconds |
| video | Optional asset and duration in seconds |
| pdf | Optional asset and physical pageCount |
| cover | Optional JPG or PNG asset |

Music accepts AAC, AIFF, FLAC, M4A, MP3 and WAV. Video accepts MP4, M4V and MOV. Supported video/audio codecs also depend on the device. Per-asset limits are 384 MiB music, 8 GiB video, 64 MiB PDF, and 8 MiB cover.

## Music and analysis

- section is T followed by a positive integer through 128 and an optional A–Z suffix for split sections. Normal values of variant are standard, alternative and bonus. The app validates native catalogue slots from the release, section, variant and any explicit catalogTrackID. Other projects are created in the user's custom projects.
- A chapter has start, end and evidence on the whole-video clock. The audioMatched evidence class requires a unit-rate window that spans the complete recording within 0.25 seconds. Other accepted evidence classes are visuallyConfirmed and approximate. Omit a chapter to leave it unresolved.
- Optional cues contain the finished action timeline ordered by time on the current recording. Each row has time in seconds, 1-based physical page, and the actual action to display. Expand structural repeats and source-supported side changes during authoring. Optional music, counts, repetitions and coaching retain the associated printed information. Optional level has red, green and blue components in [0,1] from the printed PDF. PDF pages and cue times must exist in the associated assets.
- Optional `actionRows` pairs source CTS values with exact lines in the cue's `action`. Each entry is `{"actionLine": 0, "counts": "8"}`, where `actionLine` is zero-based, includes lines with no count, and must increase. Keep the matching `counts` values in their original order, separated by newlines. Use the mapping only when every count can be placed from the source layout. Older readers can still consume the plain `counts` field.
- A cue can declare `block`, an optional integer from 1 to 128 on the first timed row of a verified block. The marked block numbers and their clocks increase within the recording. Keep all actions sharing that opening clock in one row. A completed repeat can also declare `repeatsBlock` on this same row, pointing to an earlier marked block. The App shows the source once at the block header and reads the authored actions at their signed times. Older packages without these fields remain valid.
- Optional `music[].sectionFunction` has a single-line printed function `name` and its 1-based physical PDF `page`. The name is limited to 68 characters and 240 UTF-8 bytes. Include it only with the source PDF after checking the exact recording, variant and any lettered part. The App can use this function for course display and class planning. Rehearsal PDF opening and action times remain separate fields.
- Optional pdfPage is the 1-based opening page for that recording, even when cues are unavailable. Use it for source tables containing interval durations and keep the original PDF intact. The App binds it to the exact PDF and recording and preserves a user's manual choice.

Existing package files can omit display names. File names, catalogue matching and review approval continue to use the canonical identity. An existing local project or routine keeps its name. The saved local attribution preference is outside the package. Only the selected public credit can be included.

The private plan and review files are local editing inputs. `plan.music` items use `file` for the local source path, optional `pdfPages` for cue extraction, and optional `pdfPage` for a separately verified opening page. An empty `pdfPages` list suppresses ordinary cue extraction. Use it only where the source has no extractable exercise rows. A duration-based exercise table needs the [interval and clock workflow](quality-check.md#分段时长表与曲内时钟). Approve cue rows after comparing the complete actions with the selected source and clock. The generated package contains the portable manifest and selected assets.

## Batch handoff

After reviewing cover and sources, run `ssroutine.py finish` with one or more reviewed plans. Use `ssroutine.py submit` to verify and group packages that already exist. Pass `--workspace` for the current project or `--output-dir` for an explicitly selected destination. Both produce the following layout.

    SSplayer-20261124-xxxxxxxx/
      套路资源包/
        CORE_63_2.ssroutine
      质检图/
        CORE_63_2.png
      SSplayer-20261124-xxxxxxxx.ssfingerprints.json

The batch fingerprint lists the standard name, program, release, revision, byte size, the complete package SHA-256, the raw internal manifest SHA-256 and the overview image SHA-256. Share the generated batch only as authorized. A later media or metadata change produces a new fingerprint and needs a fresh review. The reviewer can return the free `.ssapproval` file alongside the exact approved packages. Open it in SSplayer before the packages or select it in the same import. Package import requires a verified Pro entitlement.

Publishers can reuse previously signed releases by adding a verified `.ssapproval` and new fingerprint files in the Mac signer. The result covers the union of the exact file digests. Identical package entries are deduplicated. Conflicting bytes for one release and revision require a higher revision after review. The published Skill prepares fingerprints. The local publisher signs with a private Ed25519 key held outside the Skill. The approval contains the fingerprints, a public key identifier and a signature.

## Correcting an already shared package

    python3 scripts/ssroutine.py manifest CORE_63_1.ssroutine --output review-manifest.json
    python3 scripts/ssroutine.py revise CORE_63_1.ssroutine review-manifest.json --revision 2 --output-dir "/local-work/revised"
    python3 scripts/ssroutine.py submit "/local-work/revised/CORE_63_2.ssroutine" --workspace "/current-project"

Between the first two commands, correct the timelines, positions or metadata in `review-manifest.json` against the source. An ordinary revision reuses the original audio, video and PDF byte for byte. Use an explicitly approved `--cover /path/to/artwork.jpg` to change the cover, or `--without-cover` if the creator chooses to omit it.

A revision reuses each packaged recording by its verified payload hash. Rebuild from confirmed originals when restoring recordings altered in an earlier package or choosing a different source. The tool keeps every selected music recording byte for byte. Mix loudness and peak adjustment belong in SSplayer’s playlist loudness control. Existing manual preparation and notes remain with their recording in SSplayer.

### Class formats

The optional independent payload `class-templates.json` carries an object with `schemaVersion` set to 1 and a `templates` array. The package manifest binds its bytes with the `classTemplatesFile` descriptor. No device-specific recording IDs are stored. A template can be reused across routines inside its project. Each format has a stable lowercase `key`, display `name`, `minutes`, physical `page`, and ordered `slots`. Each slot lists `sectionNumbers` in the printed order of alternatives. For example `[2, 3]` is one position with two eligible source sections. A separate slot can refer to T5 after T9.

Use optional `name` for a printed function and `sectionCodes` for exact labels such as `["T8B"]`. Keep the matching `sectionNumbers`, for example `[8]`. With `selectionMode: "consecutive"`, `["T3A", "T3B"]` requires both recordings in that order from one routine. Mark a slot with `isOptional: true` when the original explicitly allows omission. Use `variantPreference` for a main or replacement preference, and `enforcesVariant` with `requiredDurationMinutes` when the source demands the exact edition. An audio item may declare `classDurationMinutes` after its edition has been verified. Keep missing required slots in the template so the App and QA can identify the gap. Previously authored packages can omit the template file and edition metadata.

The agent must compare each proposed table with its source page before approving it. A contradiction in a readable table blocks build and submission. Pages requiring visual review are indicated in QA.
