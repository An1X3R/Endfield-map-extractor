# 开发与数据接口

安装与升级见 [INSTALL.md](INSTALL.md)。以下记录源码入口与离线数据契约；公开 WebUI 按当前配置运行，不要求使用历史研究入口。

WebUI 使用锁定的 React Canary 提供 `ViewTransition`。`package.json` 仅对已验证版本的 lucide-react、motion 与 framer-motion 指定 React peer 覆盖；保留普通 `npm ci` 的其余依赖校验，不使用 `--force` 或 `--legacy-peer-deps`。升级这些库时需同步复核覆盖范围、锁文件、干净安装和界面过渡效果。

## 主要入口

- `launch_extractor.ps1`：Windows 日常启动入口，通过项目 `.venv` 调用 `launch_webui.py` 并透传参数；`-Check` 只检查现有 Python 环境，`-NoInstall` 为兼容参数且不会安装依赖。
- `launch_webui.py`：启动本机 WebUI、路径选择桥、首次运行 API 和地图导出任务服务的 Python 入口。
- `launch_first_run.ps1` / `src/endfield_first_run.py`：从只读游戏目录自动生成首次运行所需数据、审计后的 Stage 缓存和本机 runtime 配置。
- `src/webui_first_run_contract_v1.py` / `src/webui_first_run_coordinator.py`：本机首启数据准备契约与异步后端门禁；目录和完整数据未就绪时拒绝地图导出任务。
- `src/webui_export_contract_v2.py` / `schemas/webui_export_job_v2.schema.json`：Map01/Map02 的规范化异步导出请求契约。
- `src/webui_source_contract.py`：按安装结构校验用户游戏目录、外部输出边界与 Blender 路径，不要求固定盘符或安装文件夹名称。
- `src/webui_job_store.py`：在用户选择的外部输出根中保存任务状态、事件与协作式取消标记。
- `src/endfield_blender_selection_worker_v1.py`：开发研究用的版本化 Blender worker；当前公开 WebUI 不会调用它。
- `src/endfield_scene_export.py` / `src/endfield_scene_export_plan.py` / `src/blender_export_prepared_scene.py`：WebUI 的可移植场景编排、当前 ECS 精确计划与 Blender 构建入口；复用已有几何、材质和贴花实现。
- `src/build_endfield_map_layers.py` / `src/audit_endfield_map_layers.py`：构建并审计供 WebUI 使用的地图分层缓存。
- `src/extractor_core/`：只读 VFS、资源、实例、资产解析和 terrain 提取实现。
- `dotnet/`：SceneProbe 与 BundleScanner 的源码、项目文件和依赖锁；`bin/obj` 不发布。

场景首次扫描同时收录 `assets/` 和 `packages/`，地图候选仍按 `assets/` 归组。预制体边界优先复用已准备的元数据，缺失批次共享已解包依赖；正常导出按本地文件身份复用边界和选区缓存，不重复重建源层级进行深审计。跨选区模型仍按实际边界选取，显式排查时可调用 `validate_prefab_selection_evidence`；默认路径只检查保存的选区归属及基本数据结构。性能报告应区分首次准备、新选区计算和缓存复用，不能将三者作为同一测速口径。

## 武器、立绘与展示道具接口

三类资源复用已完成的角色独立准备，无需先导出大地图。后端入口为 `endfield_asset_service.py`，异步任务为 `webui_asset_worker.py`；正式 WebUI 提供独立武器工作区及角色面板中的立绘、展示道具入口。浏览器通过 `/api/v2` 代理访问下列本机接口。

| 接口 | 用途 |
| --- | --- |
| `GET /asset-catalogs/weapon` | 扫描武器中文名、原始星级、类型和可用外观资源 |
| `GET /asset-catalogs/illustration` | 扫描原尺寸透明立绘及其角色关联 |
| `GET /asset-catalogs/display_prop` | 扫描展示道具，保留配置证明的角色和挂点关联 |
| `POST /asset-jobs/validate` | 校验选择、目录身份及所需工具 |
| `POST /asset-jobs` | 提交批量异步导出，相同 UUID 和相同规范请求幂等复用 |
| `GET /asset-jobs/{request_id}` | 轮询进度、完成结果或诊断编号 |
| `POST /asset-jobs/{request_id}/cancel` | 协作式取消，保留已经生成的文件 |

全部接口沿用本机桥的 `X-Endfield-Path-Picker-Token`。请求必须包含 `format: "AssetExportRequest/1"`、UUID `request_id`、`kind`、当前 `catalog_id` 和非空不重复的 `item_ids`。`kind` 为 `weapon`、`illustration` 或 `display_prop`；武器还必须显式传 `variant: "base"` 或 `"refined_candidate"`，其他两类禁止该字段。未知字段丢弃，不接受客户端命令、工具路径或游戏资源路径。

武器选择值取 `entries[].variants[].item_id`（不是 `weapon_id`），立绘取 `entries[].illustration_id`，展示道具取 `entries[].prop_id`。缺资源或有歧义的条目保留在目录中，无法唯一解析的选择会被拒绝；`validate` 只确认请求与源关系，不代表模型已构建成功。武器 `rarity` 保留游戏表原值；强化候选只表示发现独立资源，不承诺游戏里的具体解锁条件。

任务状态为 `queued/running/completed/partial/failed/cancelled`。完成结果 `AssetExportResult/1` 包含 `kind`、`catalog_id`、`request`、`output_directory` 和逐项 `items`，必须以实际结果状态显示成功或部分完成。输出落在用户选择的角色输出目录下 `asset_jobs/{request_id}`，任务记录与诊断独立保存；中断的任务在重启后明确标记失败，不自动当作成功或继续覆盖。

立绘直接导出原始尺寸的 RGBA PNG；武器和展示道具导出内嵌贴图的静态 `.blend`。展示道具目前复用通用模型构建器，底层文件名为 `weapon.blend`，以外层 `EndfieldDisplayPropExport/1` 回执识别用途；配置挂点仅作关系证据，尚未自动挂接角色或附带展示动画。目录不暴露本机游戏和索引路径，所有关联均从当前游戏资源及配置扫描得到。

立绘结果中的 `items[].image_directory` 是 `files[].relative_path` 的解析根目录；顶层 `output_directory` 为任务目录。旧结果未提供图片目录时，前端仅显示文件名和任务目录，不猜测完整文件路径。

## 目录导航

- 根目录：公开启动器、环境引导、依赖清单和本机配置模板；启动器从仓库根目录运行。
- `src/`：运行时模块、首启与导出协调器、Blender CLI，以及 `src/extractor_core/` 共享实现。
- `tests/`：Python 回归测试。运行 `python -B -m unittest discover -s tests -t .`。
- `schemas/`：仓库维护的 JSON Schema；运行时构建会把所需 schema 复制到数据包的 `schemas/`。
- `webui/`：前端界面、本机接口适配与前端测试。
- `dotnet/`：包扫描与场景探针源码及依赖锁定文件。
- `docs/`：[首次准备契约](FIRST_RUN_BOOTSTRAP.md)。
- `fixtures/`、`patches/`、`third_party/`：测试输入、依赖补丁与第三方说明。
- 本地 `archive/local_legacy/`：已停用的历史 Stage 实验脚本，只作研究存档，不属于可移植应用，也不随源码发布。

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

正常首次运行无需手工调用本节命令：`launch_webui.py` 会先打开页面，用户在“首次运行准备”中点击“提取必要文件”后，后端才调用 `src/endfield_first_run.py`，从只读游戏目录生成 Map01/Map02 实例、资产解析、地形、场景探针数据，再构建并审计 Stage。`src/build_endfield_map_layers.py` 是面向开发者的底层重建入口，只把已提取输入转换成可供 WebUI/API 复用的只读缓存；它本身不扫描游戏目录、不启动 Blender，并拒绝复用已有输出目录。

正式输入默认包含：

- Map01 141,262 条与 Map02 361,169 条场景实例；
- Map01 WaterData sector/flowmap 候选与 Map02 大坝三张 PCG 水网格；
- 两张地图已审计的 effects 外层实例、anchor、原型与完整原型成员；
- Map01 `not_started`、Map02 `partial` 的真实灯光扫描覆盖状态。

正式构建示例：

```powershell
Set-Location '<仓库目录>'
& '.\.venv\Scripts\python.exe' '.\src\build_endfield_map_layers.py' `
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
& '.\.venv\Scripts\python.exe' '.\src\audit_endfield_map_layers.py' `
  '<外部输出目录>\stage119_standardized_map_layers_<id>' `
  --verify-source-hashes `
  --report '<外部输出目录>\stage119_standardized_map_layers_<id>\full_audit.json'
```
