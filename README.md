# Endfield extractor v2

> 项目仍在迭代。场景导出会保留未解析项与能力限制，请检查随输出生成的审计报告。

> **非官方研究工具**：本项目由《明日方舟：终末地》游戏爱好者制作，用于研究、提取和帮助二创作者。它与上海鹰角网络科技有限公司、GRYPHLINE 及其关联公司不存在隶属、合作、授权或背书关系。游戏名称、商标、图像、音频、文本、模型等属于各自权利人；本仓库只发布原创脚本与界面源码，不包含游戏资产或提取结果。请遵守适用法律、服务条款和相关授权要求。

## v2 更新

- 接通从首次数据准备到 Blender 场景导出的公开 WebUI 链路。用户自行选择游戏、Blender、缓存与输出目录，支持逐格、4×4 区块和合并选区，以及分类导出。
- 场景构建复用当前 ECS 精确引用、普通材质的 Eevee/Cycles 适配、地形图集与世界法线及投射贴花模块；保存后重开核验并打包贴图，未解析项明确保留在报告中。
- 首次准备自动生成并加载地图底图，修正雪松林缺图和北部外围裁剪。补充显示覆盖不改变 128 米分块归属及导出坐标。
- 完善首启状态切换、任务取消和输出状态展示，并补充相关契约与分块回归检查。

### 当前已知问题

v2 可用于实测，但不保证选区资产与材质完整。2026-09-24 的雪松林选区测试共生成 25 个分块，静态实例构建 13,948 / 15,226（约 91.6%）；以下是该选区的实际结果，不是全地图缺失率：

| 待处理内容 | 本次数量 | 当前状态 |
| --- | ---: | --- |
| 模型资源未进入可用缓存 | 1,209 个实例，涉及 28 种模型 | 实际缺失，以冰岩等模型为主 |
| 普通材质缺失 | 67 个实例，涉及 11 种材质 | 对应实例未构建 |
| 主模型 / LOD 选择失败 | 2 个树根实例 | 选择规则仍有缺口 |
| 贴花材质缺失 | 362 个贴花，涉及 15 种材质 | 本次选区贴花全部未恢复；模块接入不代表资源闭包完整 |
| 水、特效与灯光场景构建未接入 | 75 条提示 | 每个分块三条能力提示，不代表 75 个缺失资产 |

首次准备仍较慢，内存和磁盘占用较高，性能优化尚未实施。地形微细节和部分游戏材质、渲染管线效果仍未完整还原；水体研究节点也尚未形成任意选区自动绑定能力。报告中的 `passed` 仅指产物完整性与重开检查，`partial` 和待处理列表才反映当前资源缺口。

### 后续版本

后续修复将在 v2.1 / v2.x 中继续推进。相关计划尚未开始，具体修复项目、版本归属与排期暂不确定；本次 v2 不将计划中的工作描述为已完成。


这里是《终末地》地图数据提取、区域选择、Blender 场景与审计数据包导出 WebUI 的源码。可移植场景入口使用当前首启缓存和精确 ECS 引用，不依赖开发者的历史 `.blend`、私人目录或手工修复清单。

游戏目录必须保持只读，脚本不应修改它；每次输出都要求使用新的阶段号或时间戳，不覆盖已有产物。README 中出现的 Windows 路径只是开发环境示例，使用者应替换为自己的路径或通过对应环境变量配置。

## 发布边界与许可证

本仓库只包含提取、审计、Worker、WebUI 源码、契约、Schema、测试和包锁定文件。`.venv`、`node_modules`、构建产物、截图、日志、数据库、Blend、PNG、gzip 分层数据、本机 profile 和其他提取结果均不会发布。

实际运行时，使用者需提供 Python 3.11+、Node.js/npm、直接包含 `Endfield.exe` 的游戏根目录，以及游戏目录之外的缓存和输出目录。Stage 数据缓存、Map01/Map02 实例库、资产解析表、地形数据、Bundle/CAB 索引和场景 mesh/texture 探针由首次运行自动提取，不要求用户另行准备历史研究文件。依赖下载或某项探针失败时会保留 partial 产物并给出可恢复状态，不会伪造缺失数据。

`/jobs` 支持显式 `export_mode=blend` 和 `data_package`。场景模式要求用户选择 Blender 4.4 或更新版本并完成首次准备；服务按 sector、4×4 sector 或合并选区生成 `.blend`，使用已有 OBJ 换轴、精确材质槽、双引擎普通材质、地形图集与世界法线、投射贴花模块，保存后重新打开核验并打包贴图。未找到唯一源资产、缺材质贴图或不支持的组件会写入 `scene_plan.json` / `scene_audit.json`，不会用近似同名资产填补。历史 profile worker 仍属于独立研究入口，新链不使用它的机器路径或空 profile registry。

水面自动绑定、灯光/粒子场景重建和地形微细节输入尚未接通；选择这些分组时，数据记录与限制会保留在报告中。存在待处理项时场景结果显示 `partial`；仅选择尚不支持且没有可构建几何的分组会明确失败，不输出一个空场景冒充成功。已有水体研究节点不等于任意区域水面已可自动提取。

本项目采用 `PolyForm Noncommercial 1.0.0`，详见仓库根目录的 `LICENSE`。该许可证只覆盖本项目发布的原创软件源码，不覆盖游戏资产、第三方依赖、外部数据集或用户自行导出的文件。完整的研究与权利边界说明位于 `webui/NOTICE.md`，原创文档与界面内容的补充许可位于 `webui/CONTENT-LICENSE.md`。

## 主要入口

- `launch_webui.py`：首选入口。启动本机 WebUI、路径选择桥、首次运行 API 和地图导出任务服务。
- `launch_first_run.ps1` / `endfield_first_run.py`：从只读游戏目录自动生成首次运行所需数据、审计后的 Stage 缓存和本机 runtime 配置。
- `webui_first_run_contract_v1.py` / `webui_first_run_coordinator.py`：本机首启数据准备契约与异步后端门禁；目录和完整数据未就绪时拒绝地图导出任务。
- `webui_export_contract_v2.py` / `webui_export_job_v2.schema.json`：Map01/Map02 的规范化异步导出请求契约。
- `webui_source_contract.py`：按安装结构校验用户游戏目录、外部输出边界与 Blender 路径，不要求固定盘符或安装文件夹名称。
- `webui_job_store.py`：在用户选择的外部输出根中保存任务状态、事件与协作式取消标记。
- `endfield_blender_selection_worker_v1.py`：开发研究用的版本化 Blender worker；当前公开 WebUI 不会调用它。
- `endfield_scene_export.py` / `endfield_scene_export_plan.py` / `blender_export_prepared_scene.py`：WebUI 的可移植场景编排、当前 ECS 精确计划与 Blender 构建入口；复用已有几何、材质和贴花实现。
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

无参数启动会先打开 WebUI。请在“首次运行准备”中选择自己的游戏目录、Blender、输出目录和缓存目录，点击“校验目录”，再点击“提取必要文件”。数据包模式可以不选 Blender。准备完成且后端返回 `ready=true` 后，选区导出才会解锁。缓存和结果写入用户选择的外部路径；源码依赖按仓库位置定位。保持本地服务器运行，关闭它会停止 WebUI。

首次准备也会从本机提取的 H 瓦片生成地图底图，通过当前 runtime 的 `extraction.mapOverviews` 提供给网页。普通底图保留原始透明度，在未归属格显示有实际内容的外围瓦片；附加显示区域使用独立的已核验布局数据。显示覆盖不改写 ownershipGrid、128 米分块或导出边界。图片与缺块审计保存在外部缓存，源码不附带游戏 PNG；缺少位置证据的区域会记录为未落位。

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

地图 `/jobs` 按选区和分组写出 gzip JSONL、`selection.json`、`export_manifest.json` 与 `audit.json`。场景模式在 `scenes/batch_XXXX/` 下额外生成 `.blend`、精确构建计划、Blender 日志与重开审计；任务完成结果列出每份场景及待处理项。数据包成功不代表场景成功，Blender 非零退出或重开核验失败会使任务失败，部分产物保留供排查。

环境变量：

- `ENDFIELD_GAME_ROOT`：直接包含 `Endfield.exe` 的游戏目录。
- `ENDFIELD_CACHE_ROOT`：首次提取和可恢复中间数据目录。
- `ENDFIELD_EXPORT_ROOT`：导出根目录；详细日志固定写入其 `log` 子目录。
- `ENDFIELD_BLENDER`：Blender 可执行文件的可选启动默认值；界面中的当前选择用于场景任务。
- `ENDFIELD_EXTRACTOR_PYTHON`：可选的首选 Python 解释器；没有可用系统 Python 时仍会回退到 Blender 内嵌 Python。

## 开发研究链路与当前工作状态

资产解析的 `resolved` 表示已找到绑定候选，不代表几何或材质已完成验证。`asset_bindings.evidence_level` 区分精确名称、规范化 prefab 名称和模型家族候选；家族候选不能按精确绑定使用。解析结果记录 `asset_resolution_policy_version`，首次运行会在规则版本缺失或变化时重新生成解析结果，避免仅凭输入文件未变化而继续使用旧规则。

现有研究工具统一保留在仓库中：

| 入口 | 职责与边界 |
| --- | --- |
| `extract_projected_decals.py` / `blender_add_projected_decals.py` | 提取贴花记录并向场景接收面投影；WebUI 复用相同的解码器与投射器。 |
| `blender_apply_verified_geometry_repairs.py` | 只应用已核验的 `EndfieldGeometryRepairPlan/1` 实例清单；必须显式传入 `--builder` 指向现有研究构建脚本，以及场景、贴花和审计输入。不会自动发现修复对象。 |
| `blender_verify_geometry_repairs.py` | 重新打开修复输出，独立核对实际实例变换、材质槽、三角面数量、UV 和图片打包。 |
| `blender_build_scene_material_library.py` | 按 `Source + PathId` 选择已有 SceneProbe 材质，生成打包贴图的静态材质库；`--manifest` 可重复传入，`--selection` 使用 `EndfieldMaterialSelection/1`。 |
| `blender_apply_exact_scene_materials.py` | 按已有 `ecs_material_path` 或已保存的 `Source + PathId` 更新精确材质槽；接收 `EndfieldExactMaterialSlots/1`，保存后核对几何、UV、变换和面材质索引。 |
| `prepare_scene_material_evidence.py` | 按当前 Renderer 路径和实际保留网格，从已有 SceneProbe 缓存补全精确材质、贴图和 Prefab 证据；输出引用冲突、依赖缺漏和来源记录，不按近似名称选择材质。 |
| `extract_scene_material_dependencies.py` | 按显式 `.mat` 路径定位唯一当前资源包，沿现有 CAB 依赖追踪并校验提取公共材质依赖；缺失或歧义明确报错，结果继续交给现有 SceneProbe。 |
| `prepare_scene_geometry_repairs.py` | 将材质对应审计中失败的具体实例转换为现有修复计划；复核当前原始组件、位置和有序材质 ID，按实例区分材质变体。 |
| `extract_scene_renderer_materials.py` / `blender_restore_scene_material_slots.py` | 从已验证的实例组件提取有序材质引用，复用显式指定的旧构建器恢复白模合并前的子网格槽；只接受唯一且几何、UV 一致的对应关系。 |
| `blender_adapt_legacy_terrain_materials.py` | 复用旧构建器及已导出的地形图集，逐块核对几何后转移材质与图集 UV，并通过共享适配器恢复地形世界空间基础法线。 |
| `blender_terrain_atlas.py` / `blender_scene_terrain.py` | 按小样实际 UV 范围裁取原始图集像素，保留纹理密度与网格 UV；按源 N 页 R/G 解码世界法线，保留明确覆盖基础法线的贴花分支。微细节与陡崖投影仍待还原。 |
| `blender_refresh_projected_materials.py` | 复用现有投影器刷新已恢复贴花的接收面材质；可根据已验证的材质改动审计只刷新受影响贴花，并复核扩展缓存是否推翻此前绑定；核对贴花几何与实体集合不变。 |
| `blender_audit_scene_materials.py` | 只读重开场景，分别统计模板与实例的材质覆盖、地形及贴花数量，并核对图片已打包；覆盖率不等于 Shader 还原度。 |
| `blender_export_scene_sample.py` | 按显式随机种子、区域尺寸、实例数量和贴图体积预算，从已修复场景导出独立小样；保留原几何节点实例、UV 和打包图片，接入地形裁切及世界法线适配，重开核验并渲染预览。 |

地形提取保留原 RGB 输出，同时提供 `albedo_rgba`、`normal_rgba` 与 `detail_resources`。后者记录所用源页的 S/T/C 和该地图全部 Layers 原始 TRET 数据，保留边框、通道与 mip 链；这些源数据尚不等于已接入 Blender 的逐像素地表混合。小样导出不会缩小整张地形图集来满足体积预算，预算不足会明确报错。

材质参数契约在 `endfield_scene_material_contract.py`，Blender 节点实现在 `blender_scene_materials.py`，均不依赖区域编号或单个材质名特判。当前覆盖底色及 UV 变换、RGB/NRO 法线、MRO、三通道染色、透明裁切和发光通道遮罩。它是旧链规则的静态近似实现，保留 shader 引用和未处理属性；分层材质、地形混合、游戏光照和完整 shader 语义尚未还原。白模合并丢失的子网格材质槽需要单独恢复，已有投射贴花的接收面材质副本也需要刷新，不计入精确槽应用的完成范围。水、特效及光雾辅助对象仍按独立阶段处理。

子网格与 Renderer 材质数量不一致时，静态表面仅接受同一精确材质、完全重复的有序序列或与最后有效槽相同的尾部重复引用；原始引用完整保留，重复绘制通道不作为已还原效果。不同材质之间存在选择歧义时保持待解析，不按名称挑选版本。

材质库选择格式为 `{"format":"EndfieldMaterialSelection/1","references":[{"Source":"CAB-...","PathId":123}]}`；精确槽绑定格式为 `{"format":"EndfieldExactMaterialSlots/1","bindings":[{"container":"assets/.../material.mat","source_reference":"cab-...:123"}]}`。引用必须来自提取证据，禁止用近似名称补齐；记录冲突或选中材质的必要贴图缺失会明确报错。Unity 的 `PathId=0` 贴图引用按原始未绑定槽处理，与非零引用的导出缺失分开。材质 CLI 输出是独立研究候选，不会覆盖已有场景或自动接入 WebUI。

Blender 工具通过 `blender --background --factory-startup --python-exit-code 1 --python <脚本> -- <参数>` 运行；使用 `--help` 查看完整参数。需要显式历史输入的研究 CLI 独立于公开 WebUI；公开场景导出使用仓库内的可移植构建入口。

本机可使用忽略提交的 `reconstruction.local.json` 集中指向当前交付清单、修复计划与研究构建脚本。它是维护索引，不参与自动执行；新增结果经验证后才更新索引。游戏数据、缓存、审计和 Blender 成品继续留在配置的外部工作目录。公开 WebUI 支持 `blend` 与 `data_package`，当前完整性限制见上文“当前已知问题”。

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
