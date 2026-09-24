# Endfield Atlas Composer WebUI 2.0

本地地图提取器的 WebUI。游戏安装目录保持只读；导出任务通过 `launch_webui.py` 提供的本机桥接服务生成经过签名和审计的选区数据包，`blend` 模式同时生成逐批 Blender 场景，结果写入用户选择的输出目录。仓库不附带 Stage 数据、地图图片、游戏资产或导出结果，缺少外部输入时相关接口会明确报告不可用。

界面支持 Blender 场景与数据包两种输出。场景模式使用用户选择的 Blender 和首启缓存，调用仓库内可移植场景链；完成结果列出逐批 `.blend` 和待处理项。安装目录没有固定名称、盘符或历史文件要求。完整能力边界与构建入口见项目根目录 README；历史 profile worker 不参与这条链。

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

推荐从项目根目录运行一键启动器，以同时启用路径选择和导出任务桥：

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

生产 WebUI 默认请求同源接口。开发时若 API 运行在另一个端口，可复制
`.env.example` 为 `.env.local` 并设置 `VITE_MAP_API_ORIGIN`。

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

`vite.config.ts` 内置了仅供本地开发的同源 API 适配器：

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

## 许可与非官方声明

这是一个由《明日方舟：终末地》爱好者独立制作的非官方、非商业研究工具，
与上海鹰角网络科技有限公司、GRYPHLINE 及其关联公司不存在隶属、合作、授权或背书关系。

- 源代码采用 `PolyForm-Noncommercial-1.0.0`，仅允许该许可证定义范围内的非商业使用；
- 项目原创界面文案、文档和设计说明采用 `CC BY-NC-SA 4.0`；
- 游戏相关素材、游戏派生内容和第三方依赖不受上述项目许可覆盖；
- 完整说明见 `LICENSE`、`CONTENT-LICENSE.md`、`NOTICE.md` 以及 WebUI 底部的“研究与权利边界”。
