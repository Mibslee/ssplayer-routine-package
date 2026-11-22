# SSplayer 套路资源包制作

这个 Skill 帮教练把自己的音乐、视频、波形图和封面做成 SSplayer 可以识别的单文件。可交给支持 Agent Skills 的工具使用，也可以直接运行 Python 命令。[制作流程](SKILL.md)

## 开始制作

1. 下载仓库并把这个目录作为 Skill 交给 Agent。提供本地素材目录、项目名和套路号。
2. 安装 Python 3.10 或更新版本及 FFmpeg。用独立环境安装 Python 依赖。

       python3 -m venv .venv
       .venv/bin/python -m pip install -r requirements.txt

3. 核对逐节视频定位、原谱动作行和期望音量后，用工具制作并验证成品。成品命名例如 BC_108_1.ssroutine 或 CORE_63_1.ssroutine。
4. 在支持这个格式的 SSplayer 版本中，用已激活 Pro 的账号从「文件」或 App 的本机导入入口打开成品。

## 贡献资源包

先做一套，与开发者确认素材授权、提交方式和审核结果。累计提交 10 套不同并经开发者核验可用的套路，可兑换一个月 Pro 激活码。同一套路的更新计为同一套。制作人可在计划的 credit 字段填写公开署名。

请仅分享你有权分享的素材。通过私下约定的渠道提交样包，GitHub Issues 用于报告制作工具问题。

[文件格式](references/format.md)
