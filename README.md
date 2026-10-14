# Endfield Extractor

《明日方舟：终末地》场景与角色提取工具。通过本地 WebUI 选择内容，导出可在 Blender 中继续编辑的模型、材质、骨架与动画。

> **非官方研究工具**：本项目由《明日方舟：终末地》游戏爱好者制作，用于研究、提取和帮助二创作者。它与上海鹰角网络科技有限公司、GRYPHLINE 及其关联公司不存在隶属、合作、授权或背书关系。游戏名称、商标、图像、音频、文本、模型等属于各自权利人；本仓库只发布原创脚本与界面源码，不包含游戏资产或提取结果。请遵守适用法律、服务条款和相关授权要求。

## 支持内容

- **开放世界场景**：四号谷地、武陵，支持地图框选、分块、合并选区及分类导出。
- **独立场景**：从游戏数据发现关卡与室内场景，按名称选择导出。
- **角色与 NPC**：提取模型、材质和骨架，可按可用能力附带动作、基础表情及原始表情数据。
- **Blender 材质**：提供 Eevee / Cycles 适配，包含普通材质、角色 HGRP 风格材质，以及已支持的水体、灯光和部分特效。

角色与场景分别准备数据。游戏、Blender、缓存和输出目录均由用户选择，不要求固定安装路径。

## 开始使用

在 [Releases](https://github.com/An1X3R/Endfield-map-extractor/releases/latest) 下载 **Endfield-Extractor-v2.4-windows-x64.zip**，解压后按 [安装与升级说明](docs/INSTALL.md) 完成一次环境准备。

| 依赖 | 要求 |
|---|---|
| 系统 | Windows x64 |
| Python | 3.11+，项目依赖安装于 `.venv` |
| Blender | 4.4+，已验证 4.4.3 |
| Node.js / npm | 已验证 Node.js 24.16.0 |
| .NET | 9 x64 运行时；预编译包不需要 SDK |

发行包已包含场景与角色读取工具，无需 Git 或克隆第三方仓库。首次环境准备仍需联网安装 Python/npm 依赖；日常启动不会自动下载或安装工具。

在解压目录运行：

```powershell
powershell -ExecutionPolicy Bypass -File .\setup_release_environment.ps1
powershell -ExecutionPolicy Bypass -File .\launch_extractor.ps1
```

以后只需运行第二条启动命令。在网页中选择自己的游戏目录与输出位置，完成所需数据准备后即可导出。游戏目录始终只读。

GitHub 自动生成的 Source code ZIP 不含预编译工具。开发者可查看 [对应源码构建说明](third_party/SOURCE-BUILD.md)。

## 当前限制

- 场景、NPC、动作和材质尚未覆盖所有情况，部分内容采用 Blender 近似实现。
- 特效以静态预览为主；部分水体、灯光及动态效果与游戏仍有差异。
- 角色材质、天气和辅助变形并非完整游戏运行时，个别动作可能无法导出或出现穿模。
- `partial`／“待处理项”可能代表近似效果、未支持组件或缺失资源，**不等于全部缺资产**。具体原因随导出报告提供。
- 首次准备需要时间与磁盘空间；后续导出会复用有效缓存。实际耗时取决于选区、内容和硬件。

后续版本会继续修复和完善，具体范围尚未确定。版本变化见 [更新日志](CHANGELOG.md)，接口与数据说明见 [开发说明](docs/DEVELOPMENT.md)。

## 许可证

本项目可授权的原创软件、界面文案与项目文档采用 [MIT License](LICENSE)。第三方组件遵循各自许可证；本项目的 MIT 许可不覆盖游戏资产、第三方代码、外部数据集或用户自行导出的文件。

发行包同时提供相应开源组件的对应源码包。第三方版本与许可文本见 [第三方声明](third_party/THIRD-PARTY-NOTICES.md)；研究与权利边界见 [NOTICE](webui/NOTICE.md)，界面内容许可范围见 [CONTENT-LICENSE](webui/CONTENT-LICENSE.md)。
