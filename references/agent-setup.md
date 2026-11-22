# 安装与启动

公开名为 `ssplayer_package_creator`，智能体使用技能标识 `ssplayer-package-creator`。从 [GitHub Releases](https://github.com/Mibslee/ssplayer_package_creator/releases) 下载完整 ZIP。解开后，`SKILL.md`、`requirements.txt`、`scripts` 和 `references` 处在同一级。ZIP 用来安装 Skill，交给 SSplayer 的成品是 `.ssroutine`。

## 准备环境

用能访问素材目录和输出目录的桌面或命令行 Agent。先确认当前 Agent 的终端能运行 Python 3.10 或更新版本、FFmpeg 和 ffprobe。新装工具后重开终端，检查 PATH。[FFmpeg 下载](https://ffmpeg.org/download.html)

macOS 可通过 Homebrew 安装 FFmpeg。需要识别无文本层的 PDF 时，再装可选的 Poppler 和 Tesseract。

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

    使用 ssplayer-package-creator 制作一个 SSplayer 套路资源包。
    原始资料目录是「填写可读的完整目录」。
    输出目录是「填写最终成品目录」。
    我知道的信息是「项目、套路号、发行季度、希望使用的封面或视频版本」。
    请保留源文件，标出待确认的曲目、替换曲、AB 段、原谱页和定位。
    交付前核对实际内容、时间、音量和完整性，给我成品路径和验收结果。

## 命令行示例

在工作目录保留 `plan.json` 和 `review.json`。如有已核对的独立封面，可为 `plan` 传入 `--cover "/图片/cover.jpg"`。

    python scripts/ssroutine.py doctor
    python scripts/ssroutine.py plan "/资料/TONE 26" --program TONE --routine 26 --release-date 2024Q3 --output plan.json
    python scripts/ssroutine.py analyze plan.json --output review.json
    python scripts/ssroutine.py build review.json --output-dir "/本机工作区/成品"
    python scripts/ssroutine.py verify "/本机工作区/成品/TONE_26_1.ssroutine"

先核对 `plan.json` 中的资料和归属，再运行 `analyze`。只有查过正确录音、原谱与时间的章节和动作才在 `review.json` 中设置 `approved: true`。见[制作和质检](quality-check.md)。

修改小节、曲目版本、目录槽位或动作提取页后，更新计划并重新分析。独立的原谱打开页可在核对后填写。把完成质检的包复制到用户指定的 NAS 或网盘目录，再对目标文件执行 `verify`，核对两边的完整 SHA-256。同名文件内容不一致时使用新修订号。

## 选择视频质量

| 选项 | 视频规格 |
| --- | --- |
| `--video-quality original` | 默认沿用输入视频编码与分辨率 |
| `--video-quality ipad` | HEVC，最高 1080p，视频目标码率 1800 kb/s |
| `--video-quality compact` | HEVC，最高 720p，视频目标码率 950 kb/s |

按素材细节调整 `--video-max-height` 和 `--video-bitrate-kbps`。需要 H.264 时明确加 `--video-codec h264`。降低分辨率时保持比例，已低于上限的画面沿用原分辨率或向下取偶。画质档应用于制包，App 使用包内现成的编码。

    python scripts/ssroutine.py build review.json --output-dir "/成品" --video-quality ipad
    python scripts/ssroutine.py build review.json --output-dir "/成品" --video-quality compact --video-max-height 540 --video-bitrate-kbps 750

转码后用质检页核对实际尺寸、总码率和体积，查看运动画面的细节与小字可读性。音轨保留在秒级定位的同一时钟，制包工具核对视频每帧时间和原嵌入音频，再由离线报告复测已关联曲目。HDR、特殊画面或多个机位可选原画。发布过的套路以新修订号另存。

## 生成九宫格质检图

无需 Pro 即可在本机执行。 `--credit` 指定本次质检上屏的署名，省略时使用包内署名。若已知准确的制作时间，用 `--made-at` 给出 ISO 8601 时间，省略时显示质检生成时间。可用 `--font` 指定本机带中文的字体。

    python scripts/qa_preview.py "/成品/TONE_26_1.ssroutine" --output-dir "/成品" --credit "我的署名" --strict

输出一个同名 `.qa` 目录，包含九张单图、`3x3-overview.png` 和 `report.html`。九张图覆盖信息与封面、素材完整性、曲目/替换/分段、实际视频画面、包内音乐与视频三个窗口的对位复测、动作时间线、原谱物理页、响度解码和缺项。可视报告把复核项放在前面，每首歌可以展开看具体结果。`qa.json` 是工具间交换用的数据。

严格质检发现音乐无法解码、清单时长与实物不符、标为按音乐定位却复测失败，或已附的封面和原谱打不开时，会留下可读报告，命令以状态码 2 结束。没附的素材和未声明定位会列为复核项，允许交付已核实的内容。

把总览和可视报告作为初审材料，先核对曲目和对位，再用 Pro 验证代表性的 App 导入和排课。报告 HTML 内嵌总览，可以单独打开。带原谱或视频画面的证据使用约定渠道提交。
