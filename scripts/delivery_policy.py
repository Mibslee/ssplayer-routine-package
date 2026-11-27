"""Decide whether an offline QA result is suitable for a contributor batch."""

CHECK_NAMES = {
    "contents": "包内资料", "audio": "音乐", "video": "视频定位", "subtitles": "字幕",
    "timeline": "动作时间线", "pdf": "原版波形图", "structure": "小节功能", "formats": "课型",
}

SUPPLEMENTARY_CHECKS = {"video", "timeline", "pdf", "structure"}


def accepted_limitation(manifest, check, code, source_limited):
    if code == "supplementary_outro":
        return check in SUPPLEMENTARY_CHECKS
    if code in {"source_variant_audio_only", "source_visual_clock"}:
        return check == "video" and bool(manifest.get("video"))
    if not source_limited:
        return False
    if code == "no_timed_score":
        return check == "timeline" and bool(manifest.get("pdf"))
    if code == "no_source_video":
        return check == "video" and not manifest.get("video")
    if code == "no_source_pdf":
        return check == "pdf" and not manifest.get("pdf")
    if code == "no_source_functions":
        return check == "structure" and not manifest.get("pdf")
    if code == "no_timed_source":
        return check == "timeline" and not manifest.get("pdf")
    return False


def assess_delivery(manifest, report, *, source_limited=False):
    """Allow genuine source limitations while holding every unresolved check."""
    source_limits = {
        (row["check"], row["detail"]): row["code"]
        for row in report.get("sourceLimitations", [])
        if accepted_limitation(manifest, row["check"], row["code"], source_limited)
    }
    blockers, limitations = [], []
    for check, findings in report["checks"].items():
        blockers.extend({"check": check, "detail": detail} for detail in findings["issues"])
        for detail in findings["conditions"]:
            # qualityNotes are displayed as context, never accepted as evidence.
            if detail.startswith("制作者说明 · "):
                continue
            entry = {"check": check, "detail": detail}
            if code := source_limits.get((check, detail)):
                limitations.append({**entry, "code": code})
            else:
                blockers.append(entry)
    if blockers:
        status = "blocked"
    else:
        status = "readyWithLimitations" if limitations else "ready"
    return {"status": status, "blockers": blockers, "limitations": limitations}


def blocker_lines(delivery):
    """Render exact checks so the creator can repair the next draft."""
    return [f"{CHECK_NAMES.get(item['check'], item['check'])} · {item['detail']}"
            for item in delivery["blockers"]]


def class_slot_label(slot, *, show_edition=False):
    codes = slot.get("sectionCodes") or [f"T{number}" for number in slot["sectionNumbers"]]
    label = ("+" if slot.get("selectionMode") == "consecutive" else "/").join(codes)
    if show_edition and (edition := slot.get("requiredDurationMinutes")):
        label += f"（{edition} 分钟版）"
    return f"({label})" if slot.get("isOptional") else label


def class_slot_recordings(slot, songs):
    edition = slot.get("requiredDurationMinutes")
    codes = slot.get("sectionCodes")
    matches = {}
    for index, song in enumerate(songs):
        code = song["section"]
        if int(code[1:].rstrip("ABCDEFGHIJKLMNOPQRSTUVWXYZ")) not in slot["sectionNumbers"] or (codes and code not in codes):
            continue
        if edition is not None and song.get("classDurationMinutes") != edition:
            continue
        preference = slot.get("variantPreference")
        if slot.get("enforcesVariant") and preference in ("standard", "alternative"):
            if (song["variant"] == "standard") != (preference == "standard"):
                continue
        matches.setdefault(code, []).append(index)
    if codes and slot.get("selectionMode") == "consecutive":
        return [matches.get(code, []) for code in codes]
    return [[index for by_code in matches.values() for index in by_code]]


def class_recording_conditions(template, songs):
    missing, demands = [], []
    for slot in template["slots"]:
        if slot.get("isOptional"):
            continue
        candidates = class_slot_recordings(slot, songs)
        if all(candidates):
            demands.extend(candidates)
        else:
            label = class_slot_label(slot, show_edition=True)
            missing.append(label)
    if missing:
        return [f"{template['name']} · 缺 {'、'.join(missing)} 的录音。"]

    # Each position can use one recording. Relocate earlier choices to avoid
    # reporting a conflict when a different valid recording is available.
    assigned = {}

    def assign(position, visited):
        for track in demands[position]:
            if track in visited:
                continue
            visited.add(track)
            if track not in assigned or assign(assigned[track], visited):
                assigned[track] = position
                return True
        return False

    if all(assign(position, set()) for position in range(len(demands))):
        return []
    return [f"{template['name']} · 包内不同录音不足以填满所有必选位置，可从其他套路补选。"]
