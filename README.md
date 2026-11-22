# ssplayer_package_creator

用本地已有的音乐、视频、原版波形图和封面制作 SSplayer 可导入的单文件资源包。可以交给 WorkBuddy、DeepSeek Harness、MiniMax Code 等智能体，也可以直接运行命令行工具。

- [安装与启动](references/agent-setup.md) 包含 WorkBuddy、dsh、MiniMax Code 和命令行的具体步骤。
- [制作和质检](references/quality-check.md) 包含命名、日期、封面、替换曲、AB 段、原谱和 App 验收。
- [SKILL.md](SKILL.md) 是智能体读取的入口。下载安装时保留完整目录和脚本。
- [文件格式](references/format.md) 说明清单与兼容要求。

## 快速开始

提供原始资料目录和输出目录。告诉 Agent 已知的项目、套路号和发行时间；缺失信息可以先从来源核对。交付一个文件，例 `TONE_26_1.ssroutine`。

在支持该格式的 SSplayer 2.5.0 或更新版激活 Pro，从「文件」打开成品，或选择 App 内的本机导入入口。在资料库核对套路、分节和原谱，可在该项目下创建课单或打开排课模板。

没有 Pro 也能先做离线质检。运行 `scripts/qa_preview.py` 输出 9 张证据图、3×3 总览和可视报告 `report.html`。可填写自己的署名。对位图基于实际解码结果，报告可以逐首展开。[质检命令与说明](references/agent-setup.md#生成九宫格质检图)

## 贡献资源包

先完成一套并约定提交渠道。累计 10 套不同套路，经开发者核验可用后可兑换 1 个月 Pro 激活码。同一套路的修订沿用原记录。制作人可以填写愿意公开的署名。GitHub Issues 用于报告制作工具问题。

本仓库的 Skill 和工具可下载到个人设备或 Agent 工作区，用于制作 SSplayer 套路资源包。请通过约定渠道提交有权分享的资料，原音乐、视频、波形图和封面的权利由各素材权利人决定。
