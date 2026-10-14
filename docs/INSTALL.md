# 安装与升级

## 普通用户：预编译发行包

从 [GitHub Releases](https://github.com/An1X3R/Endfield-map-extractor/releases/latest) 下载 `Endfield-Extractor-v3-windows-x64.zip`，完整解压到可写目录。请保留 `.tools` 文件夹；GitHub 自动生成的 Source code ZIP 不包含它。

需要 Windows x64、Python 3.11+、Node.js/npm、.NET 9 x64 运行时和 Blender 4.4+（已验证 4.4.3）。读取工具已预编译，不需要 Git 或 .NET SDK。工具运行依赖以随包回执校验。

在解压目录打开 PowerShell，执行一次环境准备：

```powershell
powershell -ExecutionPolicy Bypass -File .\setup_release_environment.ps1
```

这一步会联网安装项目 Python/npm 依赖、构建界面并配置本机工具路径。依赖与缓存放在项目内；不会克隆第三方仓库或编译读取工具。若缺少前置环境，按错误提示安装后重试。

日常启动：

```powershell
powershell -ExecutionPolicy Bypass -File .\launch_extractor.ps1
```

启动不会自动安装依赖。网页打开后，选择自己的游戏目录、Blender、缓存和输出目录，按需要准备地图或角色数据；两类数据分别准备。游戏目录必须直接包含 `Endfield.exe`，缓存与输出放在游戏目录之外。提取过程保持游戏文件只读。

保持启动窗口运行；关闭它或按 Ctrl+C 会停止本机服务。默认端口是 4174，占用时选择后续空闲端口。不要用单独的 `npm run dev` 代替正式启动器，否则本机目录选择与导出服务可能不可用。

## 升级与旧缓存

建议解压新版到独立目录，完成新版环境准备，再选择原游戏、缓存和输出目录。不要直接删除旧缓存，也不要把旧版 `.tools` 覆盖到新版；工具升级会核对来源与工具身份，复用有效数据。

恢复已有地图运行配置时：

```powershell
.\launch_extractor.ps1 --runtime-config '<原有 endfield.runtime.local.json 的路径>'
```

也可把配置复制到新版根目录并保留文件名 `endfield.runtime.local.json`。移动缓存后需同步修正配置路径；配置变化后重启启动器，刷新网页不足以重载启动配置。本机配置不要随项目转发。

如果移动了整个程序目录，重新运行 `setup_release_environment.ps1` 绑定当前环境路径。保存原导出文件与外部缓存，直到确认新版可正常使用。

## 导出结果

- **导出完成**：产物已生成并通过该流程的检查。
- **导出完成，有待处理项**：产物已生成，仍有近似效果、未支持组件或缺失资源。待处理项数量不等于缺失资产数量。
- **导出失败**：查看失败阶段、错误和输出目录中的日志；失败可能保留部分产物供诊断，不能当作完整结果。

保存重开通过只证明文件可读及相应检查通过，不代表游戏画面或所有资源已经完整还原。动态特效等附属缓存应与 `.blend` 一起保留。

## 开发者：源码准备

完整对应源码包随同版本 Release 提供，包含锁定的开源上游快照、修改和许可证；构建步骤见 [SOURCE-BUILD.md](../third_party/SOURCE-BUILD.md)。它不包含游戏源码、资产、运行配置或提取缓存。

直接从 Git 仓库开发则需要 Git、.NET SDK 9.0.308，以及 Python、Node/npm 和 Blender。以下显式命令会准备开发依赖并下载锁定的开源工具来源：

```powershell
powershell -ExecutionPolicy Bypass -File .\setup_development_environment.ps1 -PrepareSceneHelpers -HelperCacheRoot .\.tools\scene
.\.venv\Scripts\python.exe -B .\src\setup_character_tools.py --work-root .\.tools\character-sources --output-root .\.tools\character --dotnet '<dotnet.exe 路径>'
.\.venv\Scripts\python.exe -B .\src\endfield_character_runtime.py --tools-receipt .\.tools\character\character_tools.json --dotnet '<dotnet.exe 路径>' --scene-helper-cache .\.tools\scene --output .\.tools\character\runtime.json
```

仅开发场景时可以不准备角色读取器。手工重建原生库还需 C++/CMake 工具，具体要求以对应源码构建说明为准。

首次地图数据准备的开发契约见 [FIRST_RUN_BOOTSTRAP.md](FIRST_RUN_BOOTSTRAP.md)，模块与离线数据接口见 [DEVELOPMENT.md](DEVELOPMENT.md)。
