# SSplayer portable routine v1

The file name is PROJECT_ROUTINE_REVISION.ssroutine. PROJECT uses 2–16 ASCII uppercase letters/digits and starts with a letter. ROUTINE is a positive number or a short uppercase release code with optional internal hyphens. REVISION is a positive integer. The manifest is the identity and may survive a Files rename.

## Container

Eight ASCII bytes SSRT0001, a four-byte unsigned big-endian JSON length, then the UTF-8 JSON manifest. The rest consists of raw file bytes in the order recorded below. There is no compression, executable metadata, encryption or file-system path in the format. Ordinary archive apps cannot expand it as a ZIP. A program implementing the format can read the payloads. SHA-256 checks content integrity against the manifest, while publisher trust and contribution approval are established outside the format.

Manifest length is at most 4 MiB. The total file is at most 12 GiB. Each asset declares lowercase ext, exact positive bytes, and lowercase whole-file sha256. Files are ordered by the music array, then optional video, PDF and cover. Missing assets consume no bytes. Approved analysis refers only to the exact media bound into the same manifest.

| Field | Meaning |
| --- | --- |
| schemaVersion | Integer 1 |
| program, routine, revision | Package identity |
| credit | Optional contributor credit, at most 128 UTF-8 bytes |
| music | 1–128 entries containing section, variant, optional catalogTrackID and title, asset, and duration in seconds |
| video | Optional asset and duration in seconds |
| pdf | Optional asset and physical pageCount |
| cover | Optional JPG or PNG asset |

Music accepts AAC, AIFF, FLAC, M4A, MP3 and WAV. Video accepts MP4, M4V and MOV. Supported video/audio codecs also depend on the device. Per-asset limits are 384 MiB music, 8 GiB video, 64 MiB PDF, and 8 MiB cover.

## Music and analysis

- section is T followed by a positive integer through 128 and an optional A–Z suffix for split sections. Normal values of variant are standard, alternative and bonus. The app validates native catalogue slots from the release, section, variant and any explicit catalogTrackID. Other projects are created in the user's custom projects.
- A chapter has start, end and evidence on the whole-video clock. The audioMatched evidence class requires a unit-rate window that spans the complete recording within 0.25 seconds. Other accepted evidence classes are visuallyConfirmed and approximate. Omit a chapter to leave it unresolved.
- Optional cues are ordered by time on the current music track. Each row has time in seconds, 1-based physical page, and action. Optional music, counts, repetitions and coaching are source text. Optional level has red, green and blue components in [0,1] from the printed PDF. PDF pages and cue times must exist in the associated assets.

The private plan and review files are editing inputs. plan.music items use file for the local source path, plus optional pdfPages for explicit source ownership. The review's approved flag is specific to that item and source; verify the source before raising it. A generated package contains only the portable manifest and supplied assets.
