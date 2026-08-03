import type { MapDefinition, MapId } from '../types'
import { REGION_MAP_RUNTIME_MANIFEST } from './regionMapRuntime'

const map01Runtime = REGION_MAP_RUNTIME_MANIFEST.maps.map01
const map02Runtime = REGION_MAP_RUNTIME_MANIFEST.maps.map02

const formatBounds = (bounds: MapDefinition['worldBounds']) =>
  `WORLD BOUNDS [${bounds.xmin}, ${bounds.xmax}) × [${bounds.zmin}, ${bounds.zmax})`

export const MAPS: Record<MapId, MapDefinition> = {
  map01: {
    id: 'map01',
    shortLabel: 'VALLEY IV',
    title: 'VALLEY IV',
    chineseName: '四号谷地',
    code: 'MAP01 / 2048',
    worldBounds: map01Runtime.worldBounds,
    grid: 128,
    direction: 'PREVIEW V ↓ / WORLD Z ↑',
    tagline: 'OPEN INDUSTRIAL FRONTIER',
    description:
      '开阔生产前线中的道路、管线与离散设施。横向机械扫描强调可达路径和网格边界。',
    warning: 'WATER / EXTERNAL CANDIDATE',
    warningTone: 'candidate',
    coordinates: formatBounds(map01Runtime.worldBounds),
  },
  map02: {
    id: 'map02',
    shortLabel: 'WULING',
    title: 'WULING',
    chineseName: '武陵',
    code: 'MAP02 / 4096',
    worldBounds: map02Runtime.worldBounds,
    grid: 128,
    direction: 'PREVIEW V ↓ / WORLD Z ↑',
    tagline: 'XIRANG GARDEN CITY',
    description:
      '未来中式城市与自然水系共生。息壤缺口方块沿庭院、台地和垂直柱阵有序生长。',
    warning: 'WATER / LOCAL RULESET',
    warningTone: 'stable',
    coordinates: formatBounds(map02Runtime.worldBounds),
  },
}

export const LAYER_LABELS = {
  terrain: ['地形基底', 'L01'],
  instances: ['场景实例', 'L02'],
  water: ['水体候选', 'L03'],
  effects: ['特效与光源', 'L04'],
} as const

export const EXPORT_GROUPS = [
  ['lighting', '光源', 'G01'],
  ['particle', '粒子', 'G02'],
  ['vegetation', '植被', 'G03'],
  ['terrain', '地形', 'G04'],
  ['road', '道路', 'G05'],
  ['water', '水体', 'G06'],
  ['effects', '特效', 'G07'],
  ['unknown', '未知', 'G08'],
] as const

export const JOB_PHASES = [
  'queued',
  'preparing',
  'extracting',
  'building',
  'auditing',
  'completed',
] as const
