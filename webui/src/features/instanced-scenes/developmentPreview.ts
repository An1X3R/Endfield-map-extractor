import type {
  InstancedSceneCatalog,
  InstancedSceneDevelopmentPreview,
  InstancedSceneExportGroup,
  InstancedSceneSummary,
} from './types'

const SAMPLE_SCENES: ReadonlyArray<Omit<InstancedSceneSummary, 'export_capability'>> = [
  { scene_id: 'dung01_cdg001', name: '压力感应', group_id: 'puzzle', form: 'maze' },
  { scene_id: 'dung01_cdg004', name: '工业化迷宫', group_id: 'puzzle', form: 'maze' },
  { scene_id: 'dung02_cdg001', name: '驱水行路', group_id: 'exploration', form: 'water' },
  { scene_id: 'dung02_cdg002', name: '需水阵列', group_id: 'exploration', form: 'water' },
  { scene_id: 'dung02_cdg007', name: '瘴中游', group_id: 'exploration', form: 'water' },
  { scene_id: 'indie_tower001', name: '战争回响', group_id: 'echo', form: 'tower' },
  { scene_id: 'indie_dg005', name: '总桩办公室', group_id: 'facilities', form: 'office' },
  { scene_id: 'base01_lv001', name: 'O.M.V.帝江号', group_id: 'facilities', form: 'ship' },
  { scene_id: 'base01_lv002', name: '模拟训练场', group_id: 'facilities', form: 'tower' },
]

const COMPONENT_COUNTS: Record<InstancedSceneExportGroup, number | null> = {
  building: null, prop: null, lighting: null, particle: null, vegetation: null,
  terrain: null, road: null, water: null, effects: null, unknown: null,
}

/** Creates explicit development metadata without pretending to export files. */
export function createInstancedSceneDevelopmentPreview(): InstancedSceneDevelopmentPreview {
  const scenes = SAMPLE_SCENES.map((scene): InstancedSceneSummary => ({
    ...scene,
    export_capability: { ready: false, reason: '开发样例不执行提取', export_modes: [] },
  }))
  const catalog: InstancedSceneCatalog = {
    format: 'InstancedSceneCatalog/1',
    catalog_id: 'development-preview-20261008',
    first_run_identity: null,
    availability: { ready: false, reason: '开发预览 · 未连接独立场景导出服务' },
    capabilities: { export_modes: [], requires_first_run: true },
    groups: [
      { group_id: 'puzzle', name: '协议空间 · 解谜' },
      { group_id: 'exploration', name: '探索与机关' },
      { group_id: 'echo', name: '战争回响' },
      { group_id: 'facilities', name: '训练与设施' },
    ],
    scenes,
  }
  return {
    catalog,
    details: scenes.map((scene) => ({
      format: 'InstancedSceneDetail/1',
      scene_id: scene.scene_id,
      catalog_id: catalog.catalog_id,
      first_run_identity: catalog.first_run_identity,
      name: scene.name,
      group_id: scene.group_id,
      source_label: '开发预览样例 / 非完整可导出目录',
      dimensions: null,
      component_counts: { ...COMPONENT_COUNTS },
      export_capability: { ...scene.export_capability },
      limitations: scene.scene_id === 'indie_dg005'
        ? ['开发样例未执行提取。按物理场景整体导出；办公室验收只覆盖室内子区。真实尺寸与构件统计由正式目录提供。']
        : ['开发样例未执行提取；真实尺寸与构件统计由正式目录提供。'],
    })),
    previews: scenes.map((scene) => scene.form === 'office' ? {
      format: 'InstancedScenePreview/1',
      scene_id: scene.scene_id,
      catalog_id: catalog.catalog_id,
      first_run_identity: catalog.first_run_identity,
      status: 'ready',
      scope: 'bounded_region',
      reason: '本地开发预览仅覆盖办公室室内子区；仅作空间验收，不代表整个物理场景或裁剪导出范围。',
      asset: {
        url: '/api/v2/dev/instanced-scene-preview/office_preview.png',
        mime: 'image/png',
        bytes: 955700,
        sha256: 'e19e88365931682df3afe71c7e4329a56947e8f649780ce457041979e4fbb0b6',
        width: 960,
        height: 600,
      },
    } : {
      format: 'InstancedScenePreview/1',
      scene_id: scene.scene_id,
      catalog_id: catalog.catalog_id,
      first_run_identity: catalog.first_run_identity,
      status: 'missing',
      scope: null,
      reason: '此样例仅显示空间意向，不代表真实模型或尺寸',
      asset: null,
    }),
  }
}
