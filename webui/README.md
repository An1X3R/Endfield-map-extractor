# Endfield Atlas Composer WebUI 2.4

本地地图提取器的 WebUI。游戏安装目录保持只读；导出任务通过 `launch_webui.py` 提供的本机桥接服务生成经过签名和审计的选区数据包，`blend` 模式同时生成逐批 Blender 场景，结果写入用户选择的输出目录。仓库不附带 Stage 数据、地图图片、游戏资产或导出结果，缺少外部输入时相关接口会明确报告不可用。

地图界面支持 Blender 场景与数据包两种输出；独立场景当前只提供 blend，选项以目录与详情返回的能力交集为准。场景模式使用用户选择的 Blender 和首启缓存，调用仓库内可移植场景链；完成结果列出逐批 `.blend` 和待处理项。安装目录没有固定名称、盘符或历史文件要求。完整能力边界与构建入口见项目根目录 README；历史 profile worker 不参与这条链。

## Stage112 地图模块

- 浏览器运行时数据：`src/data/region_map_runtime_manifest.json`
- 选区解析器：`src/lib/mapChunking.ts`
- 运行时加载边界：`src/data/regionMapRuntime.ts`
- JSON schemas 与正式样例：`contracts/`
- 原始 16 项测试：`tests/mapChunking.test.cjs`

框选统一使用 `sectorX/sectorZ = floor(world/128)`。每个 H tile 对应一个
128×128 米 sector；两张地图均为世界 `+Z` 朝画面上方；Blender 平面换轴为
`(X,Y)=(Unity X,-Unity Z)`。

三种输出模式：

- `per_sector`
- `cluster_4x4_sectors`
- `merged_selection`

选区内没有 UI 地图归属的 sector 会保留为 `coverage="unmapped"`。特效策略默认
保持 `full_system_by_anchor`。

## 启动

按[项目根目录的启动与环境准备说明](../README.md#启动)完成一次性显式准备后，从项目根目录运行启动器，以同时启用路径选择和导出任务桥。日常启动、首次游戏数据提取和导出不会自动安装依赖、下载工具或编译 .NET；缺少依赖时按错误详情中的准备命令处理：

```powershell
py .\launch_webui.py
```

仅进行前端开发时，可在本目录运行：

```powershell
npm.cmd ci
npm.cmd run dev
```

直接运行 Vite 时不会启用本机路径选择与导出 Worker。打开 `http://127.0.0.1:5173/`；关闭时在终端按 `Ctrl+C`。

## 本机地图图片接口

游戏派生 PNG 不进入源码或发布包。正式运行时需要由本机后端提供：

- `GET /api/v2/maps`
- `GET /api/v2/maps/{mapId}/overview?variant=clean|sectors`
- `POST /api/v2/maps/{mapId}/resolve-selection`

浏览器始终请求同源 `/api/v2` 和 `/api/v3`。启动器默认组合本机任务桥与地图/图层接口。
若使用独立的完整 API 网关，可复制 `.env.example` 为 `.env.local` 并设置
`ENDFIELD_BACKEND_URL`；该地址必须已提供两个 API 前缀，不能直接填写内部 Python 任务桥地址。
开发与构建预览共用相同接口适配，未知 API 返回 JSON 错误，不会返回网页 HTML。
`ENDFIELD_PREVIEW_ASSET_ROOT` 仅供开发时读取 `map01.png`、`map02.png`，与独立网关配置互斥；
它不会在构建预览中启用，也不会将图片打包进发布文件。

图片接口尚未启动时，地图自动回退到程序化场景，并显示
`FALLBACK / API REQUIRED`；这不会改变正式 sector128 框选结果。

## 构建

```powershell
npm.cmd run build
npm.cmd run preview
```

预览地址为 `http://127.0.0.1:4174/`。

Stage112 交付基线：严格验证 12/12 通过，TypeScript 行为测试 16 项通过；
1,161 tiles 精确绑定，0 重复、0 待定。

## Stage119 真实图层消费

Stage119 图层数据只通过 Vite 服务端插件读取，浏览器不会接触正式数据根、
绝对路径、gzip 分片或 Unity PathID。消费端兼容两种关键 index 形态：

- 非 shard 图层的 `records` 可以是字符串或 `{path,bytes,sha256}` descriptor；
- instances 的有效 sector 集合从 `sectorKeys`、`shards[].sectorKey` 和
  `roadShards[].sectorKey` 合并得出。

浏览器消费 schema 位于 `contracts/stage119_layer_index_consumer.schema.json`；
正式数据仍由本机服务端进行版本、哈希和路径安全门禁。

```powershell
npm.cmd run test:stage119
```

## Stage113 地图预览运行时

`vite.config.ts` 为开发服务器与构建预览提供同源 API 适配器：

- `GET /api/v2/maps`：返回不含绝对路径的安全地图目录和 overview 状态。
- `GET /api/v2/maps/{mapId}/overview?variant=clean|sectors`：读取当前 runtime 声明的底图；缺图返回 `404 map_overview_missing`，并附具体原因。
- `POST /api/v2/game-source/validate`：只读检查游戏根目录的三个必要标记。
- `POST /api/v2/map-cache-jobs`：当前返回 `501`，提示自动生成 worker 尚未接入。

首次准备从已提取的 H 瓦片生成底图，启动器通过 stage pointer 选择当前 runtime，
底图接口读取其中的 `extraction.mapOverviews.path`。仅在没有当前 runtime 时，
允许 `ENDFIELD_MAP_OVERRIDES_FILE` 显式指定开发覆盖。PNG 始终从服务端路径流式返回，
不会复制到 `src`、`public` 或 `dist`。成功响应带有 SHA256 `ETag` 与
`X-Endfield-Map-Source: dev-override|runtime-config`，界面会显示
`DEV LOCAL OVERVIEW` 或 `ACTIVE RUNTIME / READY`。

可独立验证底图接口：

```powershell
node --experimental-strip-types tests/mapOverview.contract.test.mjs
```

外部缓存使用 `EndfieldMapOverviews/1` 清单及相对 PNG 路径，要求状态为 `ready`。
普通瓦片保留透明度，未归属外围仅补充显示；经核验的附加区域由独立布局数据描述。
这些规则不修改分块归属和导出坐标。首次准备完成后自动重取底图，保留当前视角与选框。

## 独立场景前端、接口与验收状态

独立场景工作台、目录读取和任务 worker 已接入本机后端；谷地和武陵继续使用原有地图、首次准备及选区任务链。目录按物理场景去重；当前本机游戏目录快照得到 86 个物理场景，这只是该快照的结果，数量会随游戏包体和准备输入变化，不是固定契约或占位值。

设计对照以原案和最后批准的 HTML 为准，后续用户修订优先；未实现项不能据此视为已经批准删除。

| 原案要求 | 当前实现与验收状态 | 已批准的调整 / 数据边界 |
| --- | --- | --- |
| 第三胶囊与武陵间留白 40–48px、断开底线、微型 `//` 与 `IDX // DISCRETE` | 48px 独立槽位遮断底线；与两地图同级字阶、单侧 45° 切角；顶栏窄桌面分两行、胶囊主副标不换行 | 仅目录 `availability.ready=true` 时显示目录实际数量；本机当前快照为 86 个去重物理场景，目录不可用时显示 `—`，不把 86 写成固定数量 |
| 2×2 右上实心索引；未激活碳灰底/冷灰字/暗黄端点，激活亮黄底沿 | 对应几何与两种状态均已接入并完成浏览器复验 | 保留地图各自主题，不把独立黄色推广到地图内容 |
| 收束 0–200ms、基线展开 200–380ms、标题原位打散 150–300ms | App 点击链路分阶段驱动，两方向均可重播，过渡期间锁导航；状态持续挂载，浏览器复验完成 | 最终 HTML 的主标题为两行 `INSTANCED / SCENES`；减少动态效果时直接落位 |
| 工业白底、横贯工作区的透视十字/方点阵、同心测距标尺、黄地平线 | 点阵/标尺/刻度已补齐，浏览器复验完成 | 办公室截图保留原始暗场，不对白图进行伪造 |
| 默认折叠的树状目录、平滑撑高、15–20ms 子项级联、黑底白字切角选中 | 初始不选择条目已验；折叠 aria 状态、再次展开的 0/18/36ms 级联重播已验，收起撑高过渡 | 开发预览仅 9 个物理样例；生产分类/数量必须由目录返回 |
| 中央超大灰代号、中文名称、四角 HUD、边缘消隐 | 水印置标题区背后并降低对比；办公室原图已验成功读取 960×600，使用 contain 保全主体 | 有图使用同源原图；无图使用已批准抽象空间意向，真实尺寸未知显示未知，不假称精确蓝图 |
| 右上可拖动设置、键盘移动/复位、右下黄色 CTA、资产构件多选 | 拖动/键盘已验；1266×714 实测标题区 110px、设置内容区 132px、CTA 下沿 637.2px，全部首屏可用；1440×900 与 390×844 已验，手机无横向溢出 | 桌面高度 ≤780px 时标题为 110px 单行大字、设置头 68px，内容区至少 120px 并内部滚动；900px 高度保留两行标题。导出格式由独立目录与详情能力交集决定 |
| 完成方块→4:1 驻留→540° 同步化圆→黄钩破圈→上移展开报告 | 连续形变与轮廓交接已验：24px 方块、350–750ms 驻留、750–1700ms 形变、6px 柔和环；对勾 1450ms 起笔/260ms，1660ms 破口与碎屑，1720ms 上移，约 2080ms 收束 | 加粗柔和环、拉长连续形变、提前加快勾是最终用户修订；全屏震颤改为 1–2 帧局部碎屑；真实结果与操作立即可读可用 |
| 成功/部分完成/失败/取消、减少动态效果 | 状态文案：导出完成、导出完成，有待处理项、导出失败、任务已取消。success 勾为 inline，partial/failed/cancelled 勾为 none；减少动态效果已验：运行动画 0、ring/tick dashoffset 0、记录 opacity 1 | 待处理项数量不等于缺失资产数量；DEV 结果仅供视觉验收，不创建任务或伪装真实完成 |

浏览器使用同源 `/api/v2`；未配置 `ENDFIELD_BACKEND_URL` 时，Vite 本机桥把独立场景请求转发到本机 bridge 的对应路由，沿用 `ENDFIELD_PATH_PICKER_TOKEN` 并保留查询参数、上游状态码和响应正文。前端在 `webui/src/features/instanced-scenes/types.ts`、`api.ts` 校验响应身份、请求语义、进度、终态和预览地址；额外字段可忽略，缺少必需字段或身份不匹配会报错。

目录和详情遵循 `InstancedSceneCatalog/1`、`InstancedSceneDetail/1`。场景详情与预览都携带 `catalog_id` 和 `first_run_identity`，前端只接受与当前目录、首次准备身份及所选 `scene_id` 全部一致的响应；请求详情/预览时也传入 `catalog_id`。十类 `component_counts` 可为 `null`，表示尚未测量，不代表零。当前没有生产预览图：预览响应为 `status: "missing"`、`scope: null`、`asset: null`；不以办公室验收截图冒充生产场景预览。

导出请求使用 `InstancedSceneExportRequest/1`，`request_id` 是 UUID，也是 `job_id`。相同 ID 和相同规范化请求会返回现有任务；同一 ID 携带不同参数会被拒绝。刷新页面或短暂断线后，前端从 `sessionStorage` 恢复原请求并查询同一任务，任务记录持久化在当前首次准备目录下。若 bridge 进程在任务运行中退出，重启后会把未完成记录标记为 `failed` 并说明 worker 中断，不会假报仍在运行；已生成的部分产物保留。取消接口返回的是取消受理状态，最终是否取消仍以任务终态为准。

请求中的游戏目录和缓存目录必须与当前首次准备配置一致；Blender 路径需通过路径校验，输出目录必须可写且不能与游戏目录重叠。当前只支持 `blend` 导出；`data_package` 尚未实现，不能从前端类型或通用地图导出能力推断它已可用。办公室物理场景按整场导出；`indie_dg005` 的现有办公室验收只覆盖室内子区，室内子区不等于整场场景的导出范围。

| 当前路由 | 响应或请求 | 现有行为 |
| --- | --- | --- |
| `GET /api/v2/instanced-scenes` | `InstancedSceneCatalog/1` | 返回按物理场景去重的目录、分类、当前目录身份、首次准备身份及 `availability` / `capabilities`；首次准备未就绪时返回不可用目录，UI 显示未知数量。 |
| `GET /api/v2/instanced-scenes/{sceneId}?catalog_id=...` | `InstancedSceneDetail/1` | 返回场景名称、分类、尺寸、十类 nullable 构件数量、导出能力与限制；目录 ID 不匹配时拒绝读取。 |
| `GET /api/v2/instanced-scenes/{sceneId}/preview?catalog_id=...` | `InstancedScenePreview/1` | 返回预览状态、范围和资产信息；当前生产目录无预览图，使用 `missing` / `null`，不返回游戏绝对路径。 |
| `POST /api/v2/instanced-scene-jobs/validate` | `InstancedSceneExportRequest/1` → `InstancedSceneExportValidation/1` | 校验目录、首次准备身份、场景、导出组选项和路径，返回规范化请求、issues 与 warnings；失败由 HTTP 错误响应说明。 |
| `POST /api/v2/instanced-scene-jobs` | `InstancedSceneExportRequest/1` → `InstancedSceneJob/1` | 返回排队任务；以 `request_id` 幂等提交，重复同一请求返回已有任务，参数冲突拒绝。 |
| `GET /api/v2/instanced-scene-jobs/{jobId}` | `InstancedSceneJob/1` | 返回持久化任务状态、阶段、进度、请求和结果/错误；结果含文件相对路径、角色、字节数与 SHA-256。 |
| `GET /api/v2/instanced-scene-jobs/{jobId}/events?after={sequence}` | `InstancedSceneJobEvents/1` | 返回带递增序号的任务事件和 `next_after`，可从已有游标继续拉取。 |
| `POST /api/v2/instanced-scene-jobs/{jobId}/cancel` | `InstancedSceneJobCancelAck/1` | 返回已受理或任务已终态；前端继续读取任务状态，不提前宣称取消完成。 |

结果为 `InstancedSceneExportResult/1`：`status` 是 `completed` 或 `partial`，并附输出目录、已核验文件清单、十类 `component_summary` 和来源可追溯的 `pending` 项。各类 `exported`、`pending`、`unsupported` 计数允许为 `null`；`partial` 表示存在待处理项，不是缺失资产比例。无法从 plan/audit 归属到具体类别的项目计入 `unknown`，不会伪造分类数。

视觉验收记录：初始未选、首屏 CTA/设置滚动、目录展开重播、办公室截图读取、四种完成状态、减少动态效果和手机无横向溢出均已通过此前的浏览器验收；冷加载后展开目录并选择办公室时控制台无运行错误。这里的办公室截图属于开发预览素材，不是生产预览接口提供的图像。真实浏览器到本机 bridge 的独立场景导出已通过抽样：base01_lv002 生成 148.6 MiB 的 .blend、审计与计划文件，任务为 partial，42 项待处理均保留来源定位。执行中刷新后恢复同一任务编号、所选场景与输出标签，并最终显示真实结果；排队取消、运行中取消、相同请求幂等提交和文件哈希核对已通过。此验证不等于全部 86 个场景的内容还原验收；实际数量仍由当前包体决定。

开发预览需显式启用：开发服务器访问 `/?scene-preview=1`，或进入独立场景后点击“开发预览”。样例目录和结果预览不执行导出；模块由 `import.meta.env.DEV` 条件动态加载，生产构建不包含 fixture。办公室截图只从服务端环境变量 `ENDFIELD_SCENE_PREVIEW_ASSET_ROOT` 指定的外部目录读取 `office_preview.png`，路由为 `/api/v2/dev/instanced-scene-preview/office_preview.png`；仅 `configureServer` 注册，不用于构建预览，不复制到 `src`、`public` 或 `dist`。

## 角色与动作后端接口

角色准备与地图首次提取分开。启动器依次选择 `--character-config`、`ENDFIELD_CHARACTER_CONFIG`、`.tools/character/runtime.json`、仓库根目录的 `endfield.character.local.json`；当前使用 `EndfieldCharacterRuntime/2`，兼容旧版 Runtime/1；未配置时仍可使用场景。第四入口 OPERATORS 已接入角色目录、详情、分页动作、准备、导出、状态恢复与取消接口。目录默认折叠，支持名称、内部代号与拼音首字母检索；不可导出条目仍可阅读。帧预算为 2–18000，静态模型不提交动作预算；输出仅开放真实支持的 .blend 管线。任务切至其他板块后继续跟踪，取消受理后仍等后端终态。已生成文件与完整待处理清单保留在档案报告，完成动画使用已批准的通用头部 SVG 分层显影，不代表实际角色模型或游戏还原度。

角色开发视觉预览需显式访问 `/?operator-preview=1` 或点击角色板块的“开发预览”；样例模块只在开发环境动态加载，生产构建不含样例目录与结果。预览结果按钮不执行真实提取，页面明确标注 DEV。返回实时目录后恢复真实接口。角色准备弹窗保留用户填写的四项完整路径，不把后端省略路径的公开任务请求当作新的准备请求。

| 接口 | 用途 |
| --- | --- |
| `GET /api/v2/characters`、`/characters/{selection_id}` | 游戏源驱动的名称、模型变体与可用能力；不可用条目保留原因 |
| `GET /api/v2/characters/{selection_id}/animations?variant_id=...&offset=0&limit=100` | 分页获取动作候选及服务端生成的 `action_id`；候选仍需导出时绑定 Avatar |
| `POST /api/v2/character-preparation`、`/character-preparation/validate` | `CharacterPreparationRequest/1`：独立选择游戏、Blender、角色缓存和输出目录 |
| `POST /api/v2/character-jobs`、`/character-jobs/validate` | `CharacterExportRequest/1`：选择目录、角色、模型变体和输出标签 |
| `GET /api/v2/character-jobs/{id}`、`/{id}/events`；`POST /{id}/cancel` | 持久化状态、事件分页和取消；准备任务在 `character-preparation` 下提供同样入口 |

导出请求可附带 `options.raw_expressions`、`options.basic_mmd` 和 `options.animation`。动作对象要求 `action_id` 与 `maximum_stored_frames`（2–18000）；当前每次附带一个动作，超过帧预算的动作明确报错，不静默截短。表情按所选预制体的真实引用寻找面部、耳部等独立源表；基础 MMD 表情与已支持的耳控是可编辑形态键，不是 PMX 骨骼转换或全部原生表情调度。独立 NPC 每个变体对应确切预制体部件或 PrefabInfo 同时配置的完整源槽组，几何范围为 LOD0；动作绑定按所选变体验证。独立 NPC 当前不提供表情/MMD 选项，具体能力与不可用原因以目录返回为准。结果中的 `pending` 必须展示，不能把 `partial` 当成失败或完整还原。

角色、NPC、动作、表情和材质构建实现位于 `src/character_runtime/`，13 个锁定读取工具入口位于 `dotnet/CharacterTools/`。环境与工具的一次性准备、运行配置生成及第三方许可见[项目根目录说明](../README.md#启动)，客户端不要求填写 Python 脚本、DLL、Shader 或研究目录。游戏源码、反编译文本、原始 Shader、游戏资产和本机配置不进入源码或前端发布包。

动作候选仍需所选 Avatar 的绑定和导出验证；仅在后端明确返回可用能力或执行引用时开放相应选项。事件接口返回带 `next_after` / `has_more` 的分页 JSON，不是 SSE；取消受理不等于已取消，结果以任务终态为准。当前没有专门的角色预览、下载、打开输出目录或任务历史列表接口，不能仅凭结果文件扩展名推导新的导出功能。

准备请求的四项 `source` 路径均必填；当前校验响应及公开任务中的 `request` 会省略这些路径，因此不能将准备校验返回的 `canonical_request` 直接重发为准备请求。未来消费端应保留用户确认过的完整请求及其 `request_id`。不可用角色目录允许 `source_identity: null`，并可能不带 `limitations`；应按可用状态区分响应结构，不将缺失信息解释为零限制。

## 许可与非官方声明

这是一个由《明日方舟：终末地》爱好者独立制作的非官方、非商业研究工具，
与上海鹰角网络科技有限公司、GRYPHLINE 及其关联公司不存在隶属、合作、授权或背书关系。

- 本项目拥有完整授权的原创源码，以及随软件运行或构建所需的原创界面文案、项目文档和技术说明，采用 MIT License；
- MIT 不覆盖第三方上游代码及其修改、游戏相关素材或数据、外部数据集，以及用户自行生成或导出的文件；
- 第三方版本、版权、许可证/NOTICE 文本和已知待核项见 `../third_party/THIRD-PARTY-NOTICES.md` 与 `../third_party/licenses/`；
- 非官方关系和游戏权利边界声明见 `NOTICE.md` 与 WebUI 底部的“研究与权利边界”；许可范围说明见 `LICENSE` 和 `CONTENT-LICENSE.md`。
