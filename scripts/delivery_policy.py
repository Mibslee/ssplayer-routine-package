"""Decide whether an offline QA result is suitable for a contributor batch."""

CHECK_NAMES = {
    "contents": "包内资料", "audio": "音乐", "video": "视频定位", "subtitles": "字幕",
    "timeline": "动作时间线", "pdf": "原版波形图", "structure": "小节功能", "formats": "课型",
}

SUPPLEMENTARY_CHECKS = {"video", "timeline", "pdf", "structure"}


def accepted_limitation(manifest, check, code, source_limited):
    if code == "supplementary_outro":
        return check in SUPPLEMENTARY_CHECKS
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
