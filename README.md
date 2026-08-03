# Endfield extractor

> 该提取器目前仍然存在不少问题，但是基本可用，我后续可能会继续维护这个项目，但是当前我不会再进一步更新。

> **非官方研究工具**：本项目由《明日方舟：终末地》游戏爱好者制作，用于研究、提取和帮助二创作者。它与上海鹰角网络科技有限公司、GRYPHLINE 及其关联公司不存在隶属、合作、授权或背书关系。游戏名称、商标、图像、音频、文本、模型等属于各自权利人；本仓库只发布原创脚本与界面源码，不包含游戏资产或提取结果。请遵守适用法律、服务条款和相关授权要求。


这里是《终末地》地图数据提取、区域选择与审计数据包导出 WebUI 的公开源码。一次性探针、历史审计、截图和依赖本机基线的 Blender 实验脚本保留在开发者本机，不属于当前公开导出能力。

游戏目录必须保持只读，脚本不应修改它；每次输出都要求使用新的阶段号或时间戳，不覆盖已有产物。README 中出现的 Windows 路径只是开发环境示例，使用者应替换为自己的路径或通过对应环境变量配置。

## 发布边界与许可证

本仓库只包含提取、审计、Worker、WebUI 源码、契约、Schema、测试和包锁定文件。`.venv`、`node_modules`、构建产物、截图、日志、数据库、Blend、PNG、gzip 分层数据、本机 profile 和其他提取结果均不会发布。

实际运行时，使用者只需提供 Python、Node.js/npm、直接包含 `Endfield.exe` 的游戏根目录，以及游戏目录之外的缓存和输出目录。Stage 数据缓存、Map01/Map02 实例库、资产解析表、地形数据、Bundle/CAB 索引和场景 mesh/texture 探针由首次运行自动提取，不要求用户另行准备历史研究文件。依赖下载或某项探针失败时会保留 partial 产物并给出可恢复状态，不会伪造缺失数据。

当前公开版本的 `/jobs` 只导出经过签名和审计的选区数据包，不启动 Blender，也不生成 `.blend`。首次运行生成的 profile registry 明确保持 `scan.status=not_ready`，API 和 WebUI 同样返回 `blenderBuild.status=not_ready`。仓库中的 Blender worker 与历史 builder 仅保留为开发研究代码，不能在缺少可移植、已审查 profile 的情况下视为可发布构建闭包。

本项目采用 `PolyForm Noncommercial 1.0.0`，详见仓库根目录的 `LICENSE`。该许可证只覆盖本项目发布的原创软件源码，不覆盖游戏资产、第三方依赖、外部数据集或用户自行导出的文件。完整的研究与权利边界说明位于 `webui/NOTICE.md`，原创文档与界面内容的补充许可位于 `webui/CONTENT-LICENSE.md`。

## 主要入口

- `launch_webui.py`：首选入口。启动本机 WebUI、路径选择桥、首次运行 API 和地图导出任务服务。
- `launch_first_run.ps1` / `endfield_first_run.py`：从只读游戏目录自动生成首次运行所需数据、审计后的 Stage 缓存和本机 runtime 配置。
- `webui_first_run_contract_v1.py` / `webui_first_run_coordinator.py`：本机首启数据准备契约与异步后端门禁；目录和完整数据未就绪时拒绝地图导出任务。
- `webui_export_contract_v2.py` / `webui_export_job_v2.schema.json`：Map01/Map02 的规范化异步导出请求契约。
- `webui_source_contract.py`：只读游戏根目录和光源/粒子/植被等数据包导出分组的共享校验；`blender.exe` 仅作为可选未来能力校验。
- `webui_job_store.py`：在用户选择的外部输出根中保存任务状态、事件与协作式取消标记。
- `endfield_blender_selection_worker_v1.py`：开发研究用的版本化 Blender worker；当前公开 WebUI 不会调用它。
- `build_endfield_map_layers.py` / `audit_endfield_map_layers.py`：构建并审计供 WebUI 使用的地图分层缓存。
- `extractor_core/`：只读 VFS、资源、实例、资产解析和 terrain 提取实现。
- `dotnet/`：SceneProbe 与 BundleScanner 的源码、项目文件和依赖锁；`bin/obj` 不发布。

## 启动

在 Windows PowerShell 中运行：

```powershell
Set-Location '<仓库目录>'
py .\launch_webui.py
```

## 本地地图 WebUI

完整的 `WebUI 2.0` 位于 `webui/`。它是本机 `127.0.0.1` 服务：地图概览、gzip JSONL 分层数据和所有源路径都只由本机服务端读取，浏览器不会收到游戏目录或 bundle 的绝对路径。

双击或在 PowerShell 中运行：

```powershell
Set-Location '<仓库目录>'
py .\launch_webui.py
```

无参数启动会先打开 WebUI，不会在启动前要求选择目录或自动执行首次提取。请在页面左下角的“首次运行准备”中依次选择游戏目录、输出目录和缓存目录，点击“校验目录”，再点击“提取必要文件”。准备完成且后端返回 `ready=true` 后，具体地图导出按钮才会解锁。启动时出现的命令行窗口是本地服务器进程，请保持开启；关闭窗口会停止 WebUI。

首次启动会按 `package-lock.json` 执行 `npm ci`；随后会启动默认的 `http://127.0.0.1:5173/` 并自动打开浏览器。若 5173 已被占用，启动器会选择 5174–5192 内的第一个空闲端口。关闭启动窗口或按 `Ctrl+C` 即可停止 WebUI。

路径按钮通过启动器提供的本机原生选择桥工作，因此应使用 `launch_webui.py` 启动，而不是直接运行 `npm run dev`。选择桥只监听随机的 `127.0.0.1` 端口，并使用每次启动重新生成的内部令牌；游戏目录只做只读结构校验，缓存和输出目录只检查外部写入边界。用户主动选择的路径会保存在当前浏览器的本地存储中。

可选参数：

```powershell
py .\launch_webui.py --no-browser
py .\launch_webui.py --port 5180
py .\launch_webui.py --stage119-root '<外部 Stage 缓存目录>'
```

当前数据消费边界：water 仍显示为 `candidate`；首次运行未完成有效灯光闭包时，lights 保持 `not_started` 或 `partial`；roads 是点/密度视图而非道路网格。WebUI 启动器会在 Stage 缺失时调用只读首启提取链，但不会修改游戏目录。

WebUI 默认采用“先选目录、再准备数据、最后解锁地图导出”流程。token-protected 本机 bridge 提供 `/first-run`、`/first-run/validate`、`/first-run/events`、`/first-run/cancel`；请求 schema 为 `webui_first_run_job_v1.schema.json`。该接口固定运行完整 helper 与 SceneProbe 链，只有首启状态为 `completed` 时才返回 `ready: true` 并放行地图 `/jobs`。`--defer-first-run-to-webui` 仍可显式指定同一行为；仅开发者需要旧式启动前准备时使用 `--prepare-before-webui`。

地图 `/jobs` 的发布契约是 `data_package`：按选区和分组写出 gzip JSONL、`selection.json`、`export_manifest.json` 与 `audit.json`，完成哈希审计后返回 `completed`。结果中的 `blenderBuild` 固定为 `not_ready` 且 `scheduled=false`；不存在导出完成后继续调用空 profile registry 的隐式 Blender 阶段。

环境变量：

- `ENDFIELD_GAME_ROOT`：直接包含 `Endfield.exe` 的游戏目录。
- `ENDFIELD_CACHE_ROOT`：首次提取和可恢复中间数据目录。
- `ENDFIELD_EXPORT_ROOT`：导出根目录；详细日志固定写入其 `log` 子目录。
- `ENDFIELD_BLENDER`：仅供开发研究 worker 使用；当前 WebUI 数据包导出不读取或启动 Blender。
- `ENDFIELD_EXTRACTOR_PYTHON`：可选的首选 Python 解释器；没有可用系统 Python 时仍会回退到 Blender 内嵌 Python。

## Stage119 地图分层离线数据链

正常首次运行无需手工调用本节命令：`launch_webui.py` 会先打开页面，用户在“首次运行准备”中点击“提取必要文件”后，后端才调用 `endfield_first_run.py`，从只读游戏目录生成 Map01/Map02 实例、资产解析、地形、场景探针数据，再构建并审计 Stage。`build_endfield_map_layers.py` 是面向开发者的底层重建入口，只把已提取输入转换成可供 WebUI/API 复用的只读缓存；它本身不扫描游戏目录、不启动 Blender，并拒绝复用已有输出目录。

正式输入默认包含：

- Map01 141,262 条与 Map02 361,169 条场景实例；
- Map01 WaterData sector/flowmap 候选与 Map02 大坝三张 PCG 水网格；
- 两张地图已审计的 effects 外层实例、anchor、原型与完整原型成员；
- Map01 `not_started`、Map02 `partial` 的真实灯光扫描覆盖状态。

正式构建示例：

```powershell
Set-Location '<仓库目录>'
& '.\.venv\Scripts\python.exe' '.\build_endfield_map_layers.py' `
  --output-root '<外部输出目录>\stage119_standardized_map_layers_<new-id>' `
  --generated-at '2026-07-31T23:59:00+00:00'
```

测试时可以加 `--max-instances-per-map 200`。该参数只限制 instance records，asset 去重表仍会完整生成；带此参数的输出必须保持 `scan.status=partial`，不得冒充全量数据。

主要产物：

- `region_map_layer_manifest.json`：`EndfieldRegionMapLayerManifest/1` 总入口；
- `map01|map02/instances/sectors/*.jsonl.gz`：按 128 m sector 分片的实例；
- `map01|map02/effects/anchors.jsonl.gz` 与 `prototype_members.jsonl.gz`：`full_system_by_anchor` 数据；
- `map01|map02/water/records.jsonl.gz`：真实网格投影或明确标记的候选 sector-raster；
- `map01|map02/lights/index.json`：真实 records 与扫描覆盖分离，空 records 不回退为发光材质；
- `map01|map02/roads/index.json`：`instances(category=road)` 的零复制视图；`sectorKeys` 只列真实含道路记录的 sector，`roadShards` 直接引用原实例 shard；
- `source_evidence.json`、`artifact_inventory.json`、`validation.json` 和 `validation_samples.json`：来源哈希、数据指纹与可复现样例；
- `schemas/*.schema.json`：随数据一并交付的 manifest、dataset index 和 record JSON Schema。

Stage119 对浏览器消费契约做了三项兼容性修正：所有 Unity PathID 都以十进制字符串输出，避免 JavaScript 64 位整数舍入；asset table 分开记录 `lookupStatus` 与 `resolutionStatus`，并区分 `resolved`、`unresolved`、`proxy_only`、`missing_resolution`；roads 覆盖不再复制全部实例 sector。旧 Stage118 数据继续保留，不应覆盖或原地迁移。

消费端应先读取总 manifest，再按 `indexPath` 加载需要的 layer。instances 应按 `sectorKeys` 只读取命中分片；effects 选中任一成员后必须返回该 anchor 的全部 `memberEffectIds + prototypeMemberIds`；water 必须保留 `bindingStatus=candidate` 和 `geometryStatus`，不能把 WaterData 覆盖矩形当成真实水面；lights 必须同时展示 `scan.status`，不能把“当前零记录”解释成“全图没有灯光”。

Stage119 index 有两种必须区分的寻址形态：

- water/effects/lights 等非 shard 图层的 `records` 是 `{path,bytes,sha256}` descriptor，消费端必须读取 `records.path`，不能把整个对象传给路径解析器；
- instances 的正式 sector 集合以 `shards[].sectorKey` 为准，instances index 不承诺提供顶层 `sectorKeys`。roads 则使用 `roadShards[].sectorKey`，同时提供与其一致的顶层 `sectorKeys`。catalog 可把这些来源合并去重为统一的消费 DTO，但不得反向要求正式生成数据补写冗余字段。

服务端必须先验证 descriptor/path 属于 manifest 登记的 Stage 数据根，再读取 gzip JSONL；浏览器 DTO 不得暴露绝对路径、`sourcePath` 或 `meshPath`。浏览器消费 schema 位于 `webui/contracts/stage119_layer_index_consumer.schema.json`。

输入路径均可通过同名 CLI 参数或环境变量覆盖。自定义输出必须位于使用者配置的 `ENDFIELD_EXPORT_ROOT` 内，且目录必须不存在。底层构建脚本只交付离线数据；WebUI 的本地 API 由 `launch_webui.py` 启动。

全量或迁移后的缓存可用独立审计器复核。`--verify-source-hashes` 会重新读取并哈希所有外部证据；`--report` 必须指向一个不存在的新文件：

```powershell
& '.\.venv\Scripts\python.exe' '.\audit_endfield_map_layers.py' `
  '<外部输出目录>\stage119_standardized_map_layers_<id>' `
  --verify-source-hashes `
  --report '<外部输出目录>\stage119_standardized_map_layers_<id>\full_audit.json'
```
