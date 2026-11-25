# 安装与启动

公开名为 `ssplayer_package_creator`，智能体使用技能标识 `ssplayer-package-creator`。从 [GitHub Releases](https://github.com/Mibslee/ssplayer_package_creator/releases) 下载完整 ZIP。解开后，`SKILL.md`、`requirements.txt`、`scripts` 和 `references` 处在同一级。ZIP 用来安装 Skill，交给 SSplayer 的成品是 `.ssroutine`。

## 准备环境

用能访问素材目录和输出目录的桌面或命令行 Agent。先确认当前 Agent 的终端能运行 Python 3.10 或更新版本、FFmpeg 和 ffprobe。新装工具后重开终端，检查 PATH。[FFmpeg 下载](https://ffmpeg.org/download.html)

macOS 可通过 Homebrew 安装 FFmpeg。可安装 Tesseract 做画面字幕抽查。识别无文本层的 PDF 时，再装 Poppler。

    brew install ffmpeg
    brew install poppler tesseract

在 Skill 目录创建独立的 Python 环境。将下文命令里的 `python` 换成这个环境中的 Python。

    python3 -m venv .venv
    .venv/bin/python -m pip install -r requirements.txt
    .venv/bin/python scripts/ssroutine.py doctor

Windows PowerShell 可用 `py -3 -m venv .venv`，后两条命令的 Python 路径改用 `& .\.venv\Scripts\python.exe`。把已安装的 FFmpeg 和可选 OCR 工具所在目录加入 PATH。处理有文本层的 PDF 时，`doctor` 可以显示 `ocr: false`。

## WorkBuddy 桌面端

1. 在「技能」中选择「添加技能」和「上传技能」，选择完整 Skill ZIP。检查安装目录包含脚本。
2. 打开能读原始资料、能写输出目录的本地工作区，从技能工具列表选择 `ssplayer-package-creator`，或在对话中点名使用。
3. 如果当前客户端导入后找不到脚本，解压 ZIP 并让 WorkBuddy 直接读该目录的 `SKILL.md`，在同一目录运行 `doctor`。[WorkBuddy 官方技能文档](https://www.tencentcloud.com/zh/document/product/1316/84503)

## DeepSeek Harness（dsh）

1. 从所用工作区根目录开始。如果目录属于 Git 仓库，使用该仓库的根。把完整 Skill 放在 `.dsh/skills/ssplayer-package-creator/`，也可以执行下列命令。已有同名 Skill 时先核对来源，再更新。

       mkdir -p .dsh/skills
       git clone https://github.com/Mibslee/ssplayer_package_creator.git .dsh/skills/ssplayer-package-creator

2. 在同一个工作区运行已配置的 `dsh`。在会话点名使用 Skill，并给出两个路径。如没有自动发现，明确指定 `.dsh/skills/ssplayer-package-creator/SKILL.md`。[DSH 官方技能发现规则](https://github.com/deepseek-ai/deepseek-harness/blob/master/docs/subsystems/skills.md)

## MiniMax Code

1. 在桌面端选本地项目，或在工作区运行已配置的 `mcode`。把完整 Skill 放在 `.minimax/skills/ssplayer-package-creator/`，也可以执行下列命令。

       mkdir -p .minimax/skills
       git clone https://github.com/Mibslee/ssplayer_package_creator.git .minimax/skills/ssplayer-package-creator

2. 新开或重新载入会话，从中点名使用 Skill。技能列表没刷新时直接给 `SKILL.md` 路径。用能访问私有原始资料的本地工作区运行辅助工具。[MiniMax Code 官方技能目录说明](https://github.com/MiniMax-AI/mcode-stack#installation)

## 发给智能体的提示

    使用 ssplayer-package-creator 制作一批 SSplayer 套路资源包。
    原始资料目录是「填写可读的完整目录」。
    输出目录是「填写最终成品目录」。
    把识别出的项目、套路、发行时间和封面选项列成一张表，一次和我核对。
    整批视频规格和署名用「填写偏好」，单套例外在表里标出。
    保留源文件，核对替换曲、AB 段、原谱页、视频定位和动作时间线。
    完成质检后执行 submit，给我可上传的批次目录和需要了解的缺项。

## 命令行示例

在工作目录保留 `plan.json` 和 `review.json`。先把 `coverCandidates` 里的图片展示给制作者。示例里的 `/工作区/已确认.jpg` 可以是独立图片，也可以是计划提取的内嵌封面。制作者选择留空时，用 `--without-cover` 代替 `--cover`。

    python scripts/ssroutine.py doctor
    python scripts/ssroutine.py plan "/资料/TONE 26" --program TONE --routine 26 --release-date 2024Q3 --output plan.json
    python scripts/ssroutine.py select-cover plan.json --cover "/工作区/已确认.jpg" --output confirmed-plan.json
    python scripts/ssroutine.py analyze confirmed-plan.json --output review.json
    python scripts/ssroutine.py finish review.json --output-dir "/交付目录"

先核对 `plan.json` 中的资料和归属，再运行 `analyze`。通过音画复测的小节会自动确认同步定位。对需要复核的章节和 PDF 动作，比较原始资料后再批准。能点选的智能体可用「按默认制作」「修改选项」「补充要求」。也可以直接回复想用的画质、响度、署名或某一套的例外。见[制作和质检](quality-check.md)。

修改小节、曲目版本、目录槽位或动作提取页后，更新计划并重新分析。独立的原谱打开页可在核对后填写。同名文件内容不一致时使用新修订号。

## 提交一批

用 `finish` 处理一套或多套已核对的方案。进度会显示音乐、视频和质检阶段；完成后可以看到各阶段耗时。已有成品可从本机工作区选出并直接提交。

    python scripts/ssroutine.py submit "/本机工作区/成品/TONE_26_1.ssroutine" "/本机工作区/成品/CORE_63_3.ssroutine" --output-dir "/交付目录"

上传生成的整个批次目录。每套成品对应一张同名九宫格 PNG，整批只需要一份指纹文件。发给审校人的内容是网盘链接。若要核对网盘副本，从网盘读回包执行 `verify`，把输出里的完整 SHA-256 与批次指纹比较。审校人签回的 `.ssapproval` 可以和对应的资源包一起分发。

## 选择视频质量

| 选项 | 视频规格 |
| --- | --- |
| `--video-quality efficient` | 默认高画质 H.265（HEVC），保留输入分辨率和画面时钟，核验通过后选用较小成品 |
| `--video-quality original` | 沿用输入视频编码与分辨率 |
| `--video-quality ipad` | HEVC，最高 1080p，视频目标码率 1800 kb/s |
| `--video-quality compact` | HEVC，最高 720p，视频目标码率 950 kb/s |

按素材细节调整 `--video-max-height` 和 `--video-bitrate-kbps`。需要指定 H.264 时可选 iPad 或轻量档并加 `--video-codec h264`。降低分辨率时保持比例，已低于上限的画面沿用原分辨率或向下取偶。默认 H.265 档有源画面码率时会据此试压，避免低码率素材转码后反而变大。默认压缩档要求视频体积至少减少 10%，头、中、尾三个画面样本的平均 SSIM 均达到 0.96。默认档未通过时，只有源视频与要求的编码一致才会沿用源文件。源编码不同时会提示原因，可改用 iPad 或轻量档重做，或明确选择原画。质检首图显示实际打包的编码。

    python scripts/ssroutine.py build review.json --output-dir "/成品" --video-quality ipad
    python scripts/ssroutine.py build review.json --output-dir "/成品" --video-quality compact --video-max-height 540 --video-bitrate-kbps 750

转码后核对实际尺寸、总码率和体积，查看快速动作、小字及画面字幕的可读性。制包工具比较视频时间戳和音轨、内嵌字幕的数据，再由离线报告复测已关联曲目。HDR、特殊画面或多个机位可选原画。发布过的套路以新修订号另存。

## 生成九宫格质检图

无需 Pro 即可在本机执行。 `--credit` 指定本次质检上屏的署名，省略时使用包内署名。若已知准确的制作时间，用 `--made-at` 给出 ISO 8601 时间，省略时显示质检生成时间。可用 `--font` 指定本机带中文的字体。

    python scripts/qa_preview.py "/成品/TONE_26_1.ssroutine" --output-dir "/成品" --credit "我的署名" --strict

输出一个同名 `.qa` 目录，包含九张单图、`3x3-overview.png` 和 `report.html`。左上首图 `01-identity.png` 汇总套路、封面、发行时间、包版本、署名、总包大小、视频实测参数、音乐与原谱。视频有独立字幕轨时显示轨数，压进画面的字幕需看片确认。其余八图覆盖素材完整性、曲目与替换、视频画面、对位实测、动作时间线、原谱打开页、响度及复核项。可视报告把复核项放在前面，每首歌可以展开看具体结果。`qa.json` 是工具间交换用的数据。

首图沿用电影条目的阅读顺序，大封面旁是标题、发行时间和时长，下方突出包体积与离线质检结果。质检结果包含抽查视频帧和原谱打开页时发现的问题。

三张手机备课页预览都标记为「离线预览」。

- `04-video-seek.png` 从包内整课视频提取实帧，同时显示小节内时间、整课取帧时间和前中尾对位偏差。复测吻合后才按同一小节时间高亮动作。近似定位和画面定位显示视频章内时间。
- `06-action-timeline.png` 展示实际动作、拍数、重复次数和高亮行。原谱有 Level 色条时随动作显示。整段重复只在 Block 标题标一次，展开后的动作照常显示。
- `07-source-pdf.png` 优先打开所展示动作的原谱页，保留实际 PDF 页码。缺少动作时会提示可核对的原谱入口。

严格质检会阻止损坏或无法解码的文件、时长不符、声称贴合音乐却复测失败的定位。单独运行离线预览会保留报告并以状态码 2 结束。跳过未证实的位置，包内可用资料可以继续交付。

### 字幕与条件通过

- 首图核对成品有无独立字幕轨或闭路字幕，并抽查 24 帧。工具用已安装的 Tesseract 语言包查画面下方。需要中文识别时可安装简体中文语言包。抽样未检出会标明帧数。字幕可能出现在抽样时刻之外。
- 引擎不可用或有疑似字幕时，打开独立质检生成的 `subtitle-samples.jpg` 实际查看。只在核对过这份成品的画面后，用 `qa_preview.py --screen-subtitles present` 或 `--screen-subtitles absent` 重绘。如需单套提交，在 `ssroutine.py submit` 复用同一参数。
- 正常通过的栏目带绿色勾。缺视频、原谱、动作时间线或个别可靠定位时，质检图标明「条件通过」、具体受影响小节和可用资料。创作者已有明确理由时，可在 `plan` 使用 `--video-note`、`--timeline-note`，或在计划的 `qualityNotes` 写下说明。

在本机用总览和可视报告核对曲目和对位，有 Pro 时再验证 App 导入和排课。报告 HTML 内嵌总览，可以单独打开。上传时使用 `submit` 整理过的批次目录，确需补充远程排查证据时再通过约定渠道提供。
