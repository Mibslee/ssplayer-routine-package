"""Render the full offline result as a self-contained visual review page."""

from base64 import b64encode
from html import escape
from io import BytesIO
from PIL import Image

from delivery_policy import CHECK_NAMES
from subtitle_check import subtitle_summary

CSS = """
:root{color-scheme:light;--page:#eef1f5;--ink:#152338;--muted:#526178;--line:#dce3eb;
--paper:#fff;--primary:#19604c;--warn:#884006}
*{box-sizing:border-box}body{margin:0;background:var(--page);color:var(--ink);
font:15px/1.58 -apple-system,BlinkMacSystemFont,"Segoe UI","Microsoft YaHei",sans-serif}
main{max-width:1160px;margin:auto;padding:32px clamp(14px,4vw,42px) 46px}
header,.panel{background:var(--paper);border:1px solid var(--line);border-radius:16px;padding:24px;margin-bottom:20px}
h1,h2,p{margin:0}h1{font-size:clamp(30px,5vw,56px);line-height:1.13;margin:7px 0 12px}
h2{font-size:22px;margin-bottom:10px}.muted{color:var(--muted)}.kicker{letter-spacing:.08em;color:var(--primary);font-weight:700}
.stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(132px,1fr));gap:10px;margin-top:18px}
.stat{background:var(--page);border-radius:9px;padding:10px 12px}.stat strong{display:block;font-size:22px}
img{width:100%;height:auto;display:block;border-radius:10px}
details{border:1px solid var(--line);border-radius:10px;margin:8px 0;overflow:hidden}
summary{cursor:pointer;padding:12px 14px;font-weight:600}.body{padding:0 14px 14px}
.track{display:grid;grid-template-columns:minmax(95px,auto) 1fr auto;gap:10px;align-items:start}
.track span:last-child{font-weight:600;color:var(--primary)}.review span:last-child{color:var(--warn)}
dl{display:grid;grid-template-columns:max-content 1fr;column-gap:16px;row-gap:3px;margin:4px 0}
dt{color:var(--muted)}dd{margin:0;overflow-wrap:anywhere}.cues{padding-left:22px}
.note{padding:12px 14px;border-radius:8px;background:#fff6e8;color:#773600;margin-bottom:9px}
.note.blocked{background:#fff0f0;color:#91232b}.delivery{margin-top:12px;font-weight:700}
.delivery.blocked{color:#91232b}.delivery.ready{color:var(--primary)}.delivery.limited{color:var(--warn)}
footer{color:var(--muted);font-size:13px;overflow-wrap:anywhere}
@media(max-width:550px){header,.panel{padding:16px}.track{grid-template-columns:1fr auto}
.track span:nth-child(2){grid-row:2;grid-column:1/-1}.track span:last-child{grid-column:2;grid-row:1}}
"""

VARIANTS = {"standard": "正曲", "alternative": "替换曲", "bonus": "加曲"}
STATES = {"measured": "对位吻合", "review": "对位待核对", "unlocated": "无定位",
          "approximate": "近似定位", "visuallyConfirmed": "按画面定位"}


def show(value):
    return escape(str(value), quote=True)


def clock(seconds):
    whole = int(seconds)
    return f"{whole // 60}:{whole % 60:02d}"


def card(label, value):
    return f'<div class="stat"><strong>{show(value)}</strong>{show(label)}</div>'


def track_panel(track, song, has_video):
    section = f"{track['section']} · {VARIANTS.get(track['variant'], track['variant'])}"
    title = track["title"] or track["section"]
    status = STATES[track["status"]]
    needs_review = track["status"] == "review" or (has_video and track["chapter"] is None) or not track["decoded"]
    if not track["decoded"]:
        status = "解码待核对"
    rows = [("音乐时长", clock(track["duration"])),
            ("原谱入口", f"第 {track['pdfPage']} 个物理页" if track["pdfPage"] else "无指定页"),
            ("动作提示", f"{track['cueRows']} 条")]
    if function := track.get("sectionFunction"):
        function_status = track.get("sectionFunctionCheck")
        evidence = f"{function['name']} · 原谱第 {function['page']} 页"
        if function_status in {"manual", "contradiction"}:
            needs_review = True
            evidence += " · 待人工复核" if function_status == "manual" else " · 与原谱矛盾"
        rows.append(("小节功能", evidence))
    if chapter := track["chapter"]:
        rows.append(("视频位置", f"{clock(chapter['start'])}–{clock(chapter['end'])}"))
    if track["windows"]:
        rows.append(("前／中／尾偏差", " / ".join(f"{window['offsetMS']:+.1f} ms（相关 {window['correlation']:.3f}、次峰差 {window['runnerUpGap']:.3f}）"
                                                for window in track["windows"])))
    if track["decoded"]:
        rows.append(("音乐响度与峰值", f"{track['lufs']:.2f} LUFS · {track['truePeakDBTP']:.2f} dBTP"))
    content = "".join(f"<dt>{show(key)}</dt><dd>{show(value)}</dd>" for key, value in rows)
    actions = ""
    if song.get("cues"):
        entries = []
        for cue in song["cues"]:
            metadata = [f"原谱第 {cue['page']} 页"]
            if cue.get("block") is not None:
                metadata.insert(0, f"Block {cue['block']}")
            metadata.extend(str(cue[key]) for key in ("counts", "repetitions") if cue.get(key))
            details = show(" · ".join(metadata))
            note = f"<br>{show(cue['coaching'])}" if cue.get("coaching") else ""
            entries.append(f"<li><strong>{show(clock(cue['time']))}</strong> {show(cue['action'])}"
                           f"<br><span class='muted'>{details}</span>{note}</li>")
        actions = f'<details><summary>查看包内动作时间线与原谱页</summary><ol class="cues">{"".join(entries)}</ol></details>'
    cls = "track review" if needs_review else "track"
    return needs_review, (f'<details><summary class="{cls}"><span>{show(section)}</span>'
                          f'<span>{show(title)}</span><span>{show(status)}</span></summary>'
                          f'<div class="body"><dl>{content}</dl>{actions}</div></details>')


def delivery_notes(items, heading, *, blocked=False):
    if not items:
        return ""
    style = "note blocked" if blocked else "note"
    notes = "".join(
        f'<p class="{style}"><strong>{show(CHECK_NAMES.get(row["check"], row["check"]))}</strong>'
        f' · {show(row["detail"])}</p>'
        for row in items
    )
    return f'<section class="panel"><h2>{heading}</h2>{notes}</section>'


def write_html(folder, manifest, report):
    tracks = report["tracks"]
    panels = [track_panel(track, song, bool(manifest.get("video"))) for track, song in zip(tracks, manifest["music"])]
    delivery_sections = ""
    review = "".join(panel for problem, panel in panels if problem)
    if delivery := report.get("delivery"):
        status, label = {"ready": ("ready", "可提交"),
                         "readyWithLimitations": ("limited", "可提交 · 已核实原始资料限制"),
                         "blocked": ("blocked", "待修订 · 暂不可提交")}[delivery["status"]]
        verification = f'<p class="delivery {status}">{label}</p>'
        delivery_sections += delivery_notes(delivery["blockers"], "交付前需要修正", blocked=True)
        delivery_sections += delivery_notes(delivery["limitations"], "原始资料限制")
    else:
        verification = '<p class="delivery">离线质检 · 投稿资格待核验</p>'
        review = ("".join(f'<p class="note blocked">{show(issue)}</p>' for issue in report["blockingIssues"])
                  + "".join(f'<p class="note">{show(reason)}</p>'
                            for check in report["checks"].values() for reason in check["conditions"])
                  + review)
    delivery_sections += delivery_notes(report.get("advisories", []), "音频与视频观察")
    if review:
        review = f'<section class="panel"><h2>需要复核的曲目和发现</h2>{review}</section>'
    normal = "".join(panel for problem, panel in panels if not problem)
    gaps = ""
    if report["sectionGaps"]:
        numbers = "、".join(f"T{number}" for number in report["sectionGaps"])
        gaps = f'<p class="muted">包内未使用的小节编号 · {show(numbers)}</p>'
    compact = BytesIO()
    with Image.open(folder / "3x3-overview.png") as source:
        source.convert("RGB").save(compact, format="JPEG", quality=85, optimize=True)
    image = b64encode(compact.getvalue()).decode("ascii")
    identity = f"{report['program']} {report['routine']}"
    stats = "".join([card("音乐解码", f"{sum(track['decoded'] for track in tracks)}/{len(tracks)}"),
                     card("对位吻合", f"{sum(track['status'] == 'measured' for track in tracks)} 个定位"),
                     card("原谱入口", sum(bool(track["pdfPage"]) for track in tracks)),
                     card("包内动作时间线", f"{sum(track['cueRows'] > 0 for track in tracks)}/{len(tracks)} 首")])
    picture = report.get("videoInfo")
    subtitle_panel = ""
    if picture:
        video = f"{picture['width']}×{picture['height']} · {picture['codec'].upper()} · {picture['bitrateKbps']:,} kb/s"
        stats += card(f"视频 · {picture['bytes'] / (1024 * 1024):,.0f} MiB", video)
        track_subtitles, screen_subtitles = subtitle_summary(picture, report["screenSubtitles"])
        samples = folder / "subtitle-samples.jpg"
        sample_image = ""
        if samples.is_file():
            encoded = b64encode(samples.read_bytes()).decode("ascii")
            sample_image = f'<details><summary>查看有取样时间的画面</summary><img alt="字幕采样帧" src="data:image/jpeg;base64,{encoded}"></details>'
        subtitle_panel = (f'<section class="panel"><h2>字幕</h2><p>{show(track_subtitles)}</p>'
                          f'<p>{show(screen_subtitles)}</p>{sample_image}</section>')
    meta = " · ".join([report["credit"] or "未署名", f"版本 {report['revision']}",
                       report["releaseDate"] or "发行时间待核对",
                       f"{report['madeAtLabel']} {report['madeAt'][:19].replace('T', ' ')}"])
    html = f"""<!doctype html>
<html lang="zh-Hans"><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{show(identity)} · SSplayer 资源包质检</title><style>{CSS}</style>
<main>
<header><div class="kicker">SSplayer · 离线资源包质检</div><h1>{show(identity)}</h1>
<p>{show(meta)}</p><div class="stats">{stats}</div>{verification}</header>
{delivery_sections}
<section class="panel"><h2>九宫格总览</h2><img alt="九宫格质检图" src="data:image/jpeg;base64,{image}"></section>
{subtitle_panel}
{review}
<section class="panel"><h2>其他曲目</h2>{normal or '<p class="muted">曲目都已列在复核区。</p>'}{gaps}</section>
<footer><p>本页使用包内文件进行离线检查。App 导入、播放和排课结果由实际安装验收确认。</p>
<p>完整文件 SHA-256 {show(report['sha256'])}</p></footer>
</main></html>
"""
    (folder / "report.html").write_text(html, encoding="utf-8")
