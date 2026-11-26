# 安装与启动

公开名为 `ssplayer_package_creator`，智能体使用技能标识 `ssplayer-package-creator`。从 [GitHub Releases](https://github.com/Mibslee/ssplayer_package_creator/releases) 下载完整 ZIP。解开后，`SKILL.md`、`requirements.txt`、`scripts` 和 `references` 处在同一级。ZIP 用来安装 Skill，交给 SSplayer 的成品是 `.ssroutine`。

## 准备环境

用能访问素材目录和写入交付目录的桌面或命令行 Agent。原素材、构建、日志和质检都在本地处理。先确认当前 Agent 的终端能运行 Python 3.10 或更新版本、FFmpeg 和 ffprobe。新装工具后重开终端，检查 PATH。[FFmpeg 下载](https://ffmpeg.org/download.html)

macOS 可通过 Homebrew 安装 FFmpeg。Tesseract 用于扫描原谱和画面字幕抽查。需要中文等语言时安装相应语言包，用 `tesseract --list-langs` 核对。

    brew install ffmpeg
    brew install tesseract tesseract-lang

PDF 有有效文字层时直接读取原文。遇到缺少有效文字层的中文、西语等扫描件，可在当前 Agent 的终端设 `SSPLAYER_OCR_LANGUAGES=eng+chi_sim` 或 `eng+spa`，同一批的分析与质检沿用相同设置。只列出已安装且适用于这批资料的语言。

在 Skill 目录创建独立的 Python 环境。将下文命令里的 `python` 换成这个环境中的 Python。

    python3 -m venv .venv
    .venv/bin/python -m pip install -r requirements.txt
    .venv/bin/python scripts/ssroutine.py --version
    .venv/bin/python scripts/ssroutine.py doctor

Windows PowerShell 可用 `py -3 -m venv .venv`，后两条命令的 Python 路径改用 `& .\.venv\Scripts\python.exe`。把已安装的 FFmpeg 和可选 OCR 工具所在目录加入 PATH。处理有文本层的 PDF 时，`doctor` 可以显示 `ocr: false`。从项目工作目录运行时，用 Skill 里的 Python 和脚本的实际绝对路径。给含空格的路径加引号。

## WorkBuddy 桌面端

1. 在「技能」中选择「添加技能」和「上传技能」，选择完整 Skill ZIP。检查安装目录包含脚本。
2. 打开能读原始资料、能写输出目录的本地工作区，从技能工具列表选择 `ssplayer-package-creator`，或在对话中点名使用。
3. 如果当前客户端导入后找不到脚本，解压 ZIP 并让 WorkBuddy 直接读该目录的 `SKILL.md`，在同一目录运行 `doctor`。[WorkBuddy 官方技能文档](https://www.tencentcloud.com/zh/document/product/1316/84503)

## DeepSeek Harness（dsh）

1. 从所用工作区根目录开始。如果目录属于 Git 仓库，使用该仓库的根。把完整 Skill 放在 `.dsh/skills/ssplayer-package-creator/`，也可以执行下列命令。已有同名 Skill 时先核对来源，再更新。

       mkdir -p .dsh/skills
       git clone https://github.com/Mibslee/ssplayer_package_creator.git .dsh/skills/ssplayer-package-creator

2. 在项目工作区运行已配置的 `dsh`。在会话点名使用 Skill，并给出原始资料目录。已有项目工作区时可省略交付路径。如没有自动发现，明确指定 `.dsh/skills/ssplayer-package-creator/SKILL.md`。[DSH 官方技能发现规则](https://github.com/deepseek-ai/deepseek-harness/blob/master/docs/subsystems/skills.md)

升级后先用该目录的 `scripts/ssroutine.py --version` 核对实际版本。正在继续旧任务时，把新版 `SKILL.md` 的绝对路径告诉 DSH，沿用旧会话已确认的选项、原始资料和通过质检的成品。让它从阻塞项开始。

## MiniMax Code

1. 在桌面端选本地项目，或在工作区运行已配置的 `mcode`。把完整 Skill 放在 `.minimax/skills/ssplayer-package-creator/`，也可以执行下列命令。

       mkdir -p .minimax/skills
       git clone https://github.com/Mibslee/ssplayer_package_creator.git .minimax/skills/ssplayer-package-creator

2. 新开会话，在 `/skills` 确认能看到技能，再输入 `/ssplayer-package-creator`。若同名旧版仍被加载，直接给出本项目新版 `SKILL.md` 的绝对路径。让 Agent 在计划中列出所读 Skill 和本轮交付目录的绝对路径。[MiniMax Code 官方技能目录说明](https://github.com/MiniMax-AI/mcode-stack#quick-start)

## 其他 Agent 或直接使用终端

保留整份 Skill 目录，让 Agent 读 `SKILL.md`，用当地的命令执行能力运行 Python 脚本。没有技能列表、选择卡片或后台任务面板时，可以按同一流程使用文本回复和命令行。缺少本地文件权限时，换到能读写所选空间的客户端。

## 发给智能体的提示

    使用 ssplayer-package-creator 制作一批 SSplayer 套路资源包。
    原始资料目录是「填写可读的完整目录」。
    成品默认放在当前项目的「交付」文件夹。我指定其他输出目录时按本轮指示。
    把识别出的项目、套路、发行时间和封面选项列成一张表，一次和我核对。
    整批视频规格和署名用「填写偏好」，单套例外在表里标出。
    保留源文件，核对替换曲、AB 段、小节功能及原谱页、视频定位和动作时间线。
    通过 check-review 后执行 finish，返回本地批次目录、素材限制和实际验收范围。

## 命令行示例

在工作目录保留 `plan.json` 和 `review.json`。下列脚本路径相对于 Skill 目录，工作文件也可替换为实际绝对路径。先展示 `coverCandidates` 里的图片。示例中的封面可以是独立图片、计划提取的内嵌封面或已确认的文字封面。

    python scripts/ssroutine.py doctor
    python scripts/ssroutine.py attribution
    python scripts/ssroutine.py plan "/资料/TONE 26" --program TONE --routine 26 --release-date 2024Q3 --output plan.json
    python scripts/ssroutine.py confirm-plan plan.json --project-name "综合训练" --routine-name "TONE 第 26 期" --cover "/工作区/已确认.jpg" --credit "我的署名" --remember-credit --output confirmed-plan.json
    python scripts/ssroutine.py analyze confirmed-plan.json --output review.json
    python scripts/ssroutine.py check-review review.json
    python scripts/ssroutine.py finish review.json --workspace "/当前项目工作目录"

在创建 MiniMax Code 等项目时已选工作文件夹的，直接用该目录。提前显示完整的交付路径，放进原有的批次确认中。用户本轮明确指定其他交付位置时，用 `--output-dir`。历史批次位置只用于查阅已有成品。

首次选择公开署名时，请制作者填写实际显示的名字。若本轮已经给出确切署名，直接沿用。选择「公开并设为默认」时如姓名留空，先补问姓名，再运行 `confirm-plan --credit "已确认的署名" --remember-credit`。默认不署名用 `--no-credit --remember-credit`。以后只需在批次确认里显示已存选择，制作者可以沿用或修改。仅本批改名或留空时，用 `--credit` 或 `--no-credit`。用 `python scripts/ssroutine.py attribution --reset` 清除本机默认。配置默认位于 `~/.config/ssplayer_package_creator/attribution.json`，支持 `XDG_CONFIG_HOME`。

制作者没有封面时，生成文字预览并展示。确认使用后，把 PNG 交给同一个 `--cover` 选项。图片会进入资源包和九宫格首图。可以从计划读取名称，也可以用下面的参数应用本轮改名。当前字体缺少字符时，用 `--font` 指定支持课程文字的字体，例如 Noto Sans CJK。

    python scripts/ssroutine.py text-cover plan.json --project-name "综合训练" --routine-name "第 26 期" --output cover-preview.png
    python scripts/ssroutine.py confirm-plan plan.json --project-name "综合训练" --routine-name "第 26 期" --cover cover-preview.png --no-credit --output confirmed-plan.json

制作者选择留空时使用 `--without-cover`。旧版已核验的计划和资源包仍可处理。更改项目或套路身份、显示名称、版本、发行时间、封面或本批署名后，重新记录确认。采用文字封面时，改名也需要重新生成 PNG。

先核对 `plan.json` 中的资料和归属，再运行 `analyze`。通过音画复测的小节会自动确认同步定位。对需要复核的章节和 PDF 动作，比较原始资料后再批准。确认里显示「音乐：原样保留」。能点选的智能体可用「确认开始」「调整选项」「输入要求」。制作者可以直接回复调整画质、署名或当批素材范围。见[制作和质检](quality-check.md)。

修改小节、曲目版本、目录槽位或动作提取页后，更新计划，运行 `analyze --output review-v2.json`。保留之前的复核文件，对新结果重新核实受影响的小节。独立的原谱打开页可在核对后填写。同名成品内容不一致时使用新修订号。

选定多份候选原谱中的一份时，用 `plan --score-pdf "/原始资料/中文.pdf"`。双语合册可用 `--cue-pages 6-34 --class-format-pages 5` 指定实际 PDF 中的物理页。范围以本册为准，续页也要纳入。跨布局或不同语言时，先认清是动作表、课型表、技术说明还是图例，再确认动作与录音的关系。少量水印文字的扫描页会自动进入 OCR。抽取结果待视觉核实，确认后写入复核文件。

## 提交一批

用 `finish` 处理一套或多套已核对的方案。进度会显示音乐、视频和质检阶段；完成后可以看到各阶段耗时。已有成品可从本机工作区选出并直接提交。

先根据 `check-review` 指出的小节补齐证据。工具会核对原谱功能、课型顺序及必选位置能否填满。用 `check-review reviewed.json --measure-video` 复测已确认的音频匹配。遇到固定偏移、持续漂移或只能按画面跳章，按[音画校时指南](video-clock.md)记录相应证据。制作者明确选择适用于原始资料的有限范围后，可对 `check-review`、`finish` 或 `submit` 传 `--source-limited`。质检仍会核对已提供的素材。

让智能体直接显示 `finish` 的标准错误进度，或写入日志后按需查看。长视频会报告编码百分比。已完成的资源包使用 `submit` 接续，省去重新编码。生成正式批次时，`finish` 已包含 `submit` 和九宫格质检。

    python scripts/ssroutine.py submit "/本机工作区/成品/TONE_26_1.ssroutine" "/本机工作区/成品/CORE_63_3.ssroutine" --workspace "/当前项目工作目录"

多套制作时加 `--keep-going`。工具先核对所有方案，再逐套构建和质检。仍有阻塞时会给出 `partial` 汇总，指纹只包含通过质检的包。全部阻塞时不生成批次。下一次可合并已完成的成品和新修好的方案，重复传 `--include-package` 加入已有包，并使用新的批次 ID。已有批次和指纹保留。

    python scripts/ssroutine.py finish review-fixed.json --include-package "/先前成品/TONE_26_1.ssroutine" --workspace "/当前项目工作目录" --batch-id TONE-combined-2 --keep-going

多个成品已逐一看过抽样帧时，可为 `finish` 或 `submit` 提供 `--subtitle-observations captions.json`。JSON 的键是规范包名，每项提供该包完整 SHA-256 和 `status`（`present` 或 `absent`）。哈希可从 `verify` 的输出读取。只有匹配同一份成品的记录可以通过该项确认，未填写的包继续自动抽查。

完成后返回本地批次目录。每套成品对应一张同名九宫格 PNG，整批只需一份指纹文件。用户明确指定上传的批次和目的地后，再分享有权分发的文件。若要核对网盘副本，从网盘读回包执行 `verify`，把输出里的完整 SHA-256 与批次指纹比较。原始素材、计划、完整报告和测试文件留在本机。

### 失败恢复与局部修订

| 情况 | 下一步 |
| --- | --- |
| `check-review` 未通过 | 根据列出的小节找对应动作、音画或课型证据，补齐后再检查。 |
| 包已生成，批次验收失败 | 使用工具报告的保留路径，对同一成品运行 `qa_preview.py ... --strict`，打开报告及抽样帧。只有字幕判定变化时，对同一成品用 `submit --screen-subtitles present` 或 `absent`。 |
| 动作、打开页或章节要改 | 提取旧包清单，校正字段，用 `revise` 提高修订号，复核后重新 `submit`。旧版视频和音频会直接复用。 |
| 源录音真峰值超出 0 dBTP | 如实记录在哪一首、峰值是多少。沿用原音频字节，Mix 播放时在 App 的「歌单响度一致化」处理。 |
| 旧包里的录音曾经被改过音量或速度 | 从已核实的原始录音重新制作。每首录音核对原文件与包内文件的 SHA-256。 |
| 换了一首原始录音、视频或原谱 | 重新匹配对应来源，生成新复核文件后构建。 |

`finish`、`build` 和 `revise` 都保留所用音乐的原始字节。旧脚本里的降峰、响度归一和视频音轨均衡参数要从制包命令移除。质检会给出解码、时长、LUFS 和真峰值。单独的源峰值提示不降低交付结论。

把老 CD 和新发行混排在同一份 Mix 时，在 App 试听交接点。需要处理音量或峰值时，使用歌单音量栏旁的「歌单响度一致化」，按这份 Mix 的播放情况调整。

[命令与修订字段](format.md#correcting-an-already-shared-package)

## 选择视频质量

| 选项 | 视频规格 |
| --- | --- |
| `--video-quality efficient` | 默认高画质 H.265（HEVC），保留输入分辨率和画面时钟，核验通过后选用较小成品 |
| `--video-quality original` | 沿用输入视频编码与分辨率 |
| `--video-quality ipad` | HEVC，最高 1080p，视频目标码率 1800 kb/s |
| `--video-quality compact` | HEVC，最高 720p，视频目标码率 950 kb/s |

按素材细节调整 `--video-max-height` 和 `--video-bitrate-kbps`。需要指定编码时用 `--video-codec hevc` 或 `h264`。降低分辨率时保持比例，已低于上限的画面沿用原分辨率或向下取偶。默认压缩档尝试 H.265，要求视频体积至少减少 10%，头、中、尾三个画面样本的平均 SSIM 均达到 0.96，并验证音轨、独立字幕和时钟。默认方案且没有指定编码、分辨率或码率时，未通过的压缩尝试可保留兼容的原视频，构建日志显示原因。明确指定的参数继续约束输出。质检首图显示实际打包的编码。

    python scripts/ssroutine.py build review.json --output-dir "/成品" --video-quality ipad
    python scripts/ssroutine.py build review.json --output-dir "/成品" --video-quality compact --video-max-height 540 --video-bitrate-kbps 750

转码后核对实际尺寸、总码率和体积，查看快速动作、小字及画面字幕的可读性。制包工具比较视频时间戳和音轨、内嵌字幕的数据，再由离线报告复测已关联曲目。HDR、特殊画面或多个机位可选原画。发布过的套路以新修订号另存。

## 生成九宫格质检图

无需 Pro 即可在本机执行。正式批次质检使用包内署名。单独预览可用 `--credit` 临时标注。若已知准确的制作时间，用 `--made-at` 给出 ISO 8601 时间，省略时显示质检生成时间。可用 `--font` 指定本机带中文的字体。

    python scripts/qa_preview.py "/成品/TONE_26_1.ssroutine" --output-dir "/成品" --strict

输出一个同名 `.qa` 目录，包含九张单图、`3x3-overview.png` 和 `report.html`。左上首图 `01-identity.png` 汇总套路、封面、发行时间、包版本、署名、总包大小、视频实测参数、音乐与原谱。视频有独立字幕轨时显示轨数，压进画面的字幕需看片确认。其余八图覆盖素材完整性、曲目及小节功能、视频画面、对位实测、动作时间线、原谱打开页、响度和课型识别。可视报告把复核项放在前面，每首歌可以展开看具体结果。`qa.json` 是工具间交换用的数据。

首图沿用电影条目的阅读顺序，大封面旁是标题、发行时间和时长，下方突出包体积与离线质检结果。质检结果包含抽查视频帧和原谱打开页时发现的问题。

三张手机备课页预览都标记为「离线预览」。

- `04-video-seek.png` 从包内整课视频提取实帧，同时显示小节内时间、整课取帧时间和前中尾对位偏差。复测吻合后才按同一小节时间高亮动作。近似定位和画面定位显示视频章内时间。
- `03-track-list.png` 标明每首录音已核实的小节功能和实际原谱页。功能缺少证据时，报告列出具体小节和待核对的内容。
- `06-action-timeline.png` 展示实际动作、拍数、重复次数和高亮行。原谱有 Level 色条时随动作显示。整段重复只在 Block 标题标一次，展开后的动作照常显示。
- `07-source-pdf.png` 优先打开所展示动作的原谱页，保留实际 PDF 页码。缺少动作时会提示可核对的原谱入口。

严格质检会阻止损坏文件、时长不符、音画复测失败和可补齐的教学缺项。遇到阻断时，单独运行离线预览会保留报告，并以状态码 2 结束。用报告定位问题后，再决定修订原包还是重新构建。

### 字幕与交付范围

- 首图核对成品有无独立字幕轨或闭路字幕，并抽查 24 帧。工具用已安装的 Tesseract 语言包查画面下方。需要中文识别时可安装简体中文语言包。抽样未检出会标明帧数。字幕可能出现在抽样时刻之外。
- 引擎不可用或有疑似字幕时，打开独立质检生成的 `subtitle-samples.jpg` 实际查看。只在核对过这份成品的画面后，用 `qa_preview.py --screen-subtitles present` 或 `--screen-subtitles absent` 重绘。如需单套提交，在 `ssroutine.py submit` 复用同一参数。
- 已核实的栏目带绿色勾。可修复的缺项会暂缓交付。经制作者确认且能从源资料核实的限制会单独显示。可在 `plan` 用 `--video-note`、`--timeline-note` 或 `qualityNotes` 补充背景。

在本机用总览和可视报告核对曲目和对位，有 Pro 时再验证 App 导入和排课。报告 HTML 内嵌总览，可以单独打开。对外提供内容时，遵循用户本轮指定的文件和目的地。
