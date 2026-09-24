import { useEffect, useRef, useState } from 'react'
import {
  Application,
  Container,
  FederatedPointerEvent,
  Graphics,
  Rectangle,
  Sprite,
  Text,
  TextStyle,
  Texture,
} from 'pixi.js'
import { AnimatePresence, motion } from 'motion/react'
import { Crosshair, Maximize2, Minus, Plus } from 'lucide-react'
import { MAPS } from '../data/maps'
import {
  getRuntimeMap,
  REGION_MAP_RUNTIME_MANIFEST,
  resolveRuntimeAssetUrl,
  shouldRequestRuntimeOverview,
} from '../data/regionMapRuntime'
import {
  fetchRuntimeLayer,
  type RuntimeLayerId,
  type RuntimeLayerRecord,
  type RuntimeLayerResponse,
} from '../data/regionMapLayers'
import {
  resolveSelection,
  screenDragToWorldBounds,
} from '../lib/mapChunking'
import type {
  ChunkMode,
  ChunkSelection,
  RuntimeMap,
  ScreenViewport,
} from '../lib/mapChunking'
import type { LayerState, MapId } from '../types'

type Props = {
  mapId: MapId
  layers: LayerState
  chunkMode: ChunkMode
  selection: ChunkSelection | null
  overviewRevision: string
  onSelectionChange: (selection: ChunkSelection | null) => void
  onOverviewSourceChange?: (source: 'loading' | 'dev-override' | 'runtime-config' | 'fallback') => void
}

type AnimatedTargets = {
  scanner: Graphics | null
  pulseNodes: Graphics[]
  xirangBlocks: Graphics[]
  waterSignals: Graphics[]
  overviewSprite: Sprite | null
}

type RuntimeLayerData = Partial<Record<RuntimeLayerId, RuntimeLayerResponse>>

const clamp = (value: number, min: number, max: number) =>
  Math.min(max, Math.max(min, value))

const line = (
  parent: Container,
  points: readonly number[],
  color: number,
  width: number,
  alpha = 1,
) => {
  const graphic = new Graphics()
  graphic.moveTo(points[0], points[1])
  for (let index = 2; index < points.length; index += 2) {
    graphic.lineTo(points[index], points[index + 1])
  }
  graphic.stroke({ color, width, alpha })
  parent.addChild(graphic)
  return graphic
}

const polygon = (
  parent: Container,
  points: readonly number[],
  color: number,
  alpha = 1,
) => {
  const graphic = new Graphics()
  graphic.poly([...points]).fill({ color, alpha })
  parent.addChild(graphic)
  return graphic
}

const dot = (
  parent: Container,
  x: number,
  y: number,
  radius: number,
  color: number,
  alpha = 1,
) => {
  const graphic = new Graphics()
  graphic.circle(x, y, radius).fill({ color, alpha })
  parent.addChild(graphic)
  return graphic
}

function drawGrid(
  parent: Container,
  width: number,
  height: number,
  map: RuntimeMap,
  mapId: MapId,
) {
  const grid = new Graphics()
  const columns = map.sideSectors
  const rows = columns

  for (let index = 0; index <= columns; index += 1) {
    const x = (width / columns) * index
    grid.moveTo(x, 0).lineTo(x, height)
  }
  for (let index = 0; index <= rows; index += 1) {
    const y = (height / rows) * index
    grid.moveTo(0, y).lineTo(width, y)
  }
  grid.stroke({
    color: mapId === 'map01' ? 0x26302f : 0x285347,
    width: 1,
    alpha: mapId === 'map01' ? 0.085 : 0.075,
  })
  parent.addChild(grid)

  const majors = new Graphics()
  for (let index = 0; index <= columns; index += 4) {
    const x = (width / columns) * index
    majors.moveTo(x, 0).lineTo(x, height)
  }
  for (let index = 0; index <= rows; index += 4) {
    const y = (height / rows) * index
    majors.moveTo(0, y).lineTo(width, y)
  }
  majors.stroke({
    color: mapId === 'map01' ? 0x26302f : 0x285347,
    width: 1,
    alpha: 0.16,
  })
  parent.addChild(majors)
}

function drawRuntimeOwnership(
  parent: Container,
  width: number,
  height: number,
  map: RuntimeMap,
  mapId: MapId,
) {
  const overlay = new Graphics()
  const cellWidth = width / map.sideSectors
  const cellHeight = height / map.sideSectors
  const baseColor = mapId === 'map01' ? 0xe1cb00 : 0x56c99c

  map.ownershipGrid.forEach((row, southToNorthIndex) => {
    row.forEach((levelId, column) => {
      if (!levelId) return
      const screenRow = map.sideSectors - 1 - southToNorthIndex
      overlay
        .rect(
          column * cellWidth + 1,
          screenRow * cellHeight + 1,
          Math.max(1, cellWidth - 2),
          Math.max(1, cellHeight - 2),
        )
        .fill({ color: baseColor, alpha: 0.055 })
    })
  })

  parent.addChild(overlay)
}

const projectRuntimePoint = (
  point: { x: number; z: number },
  width: number,
  height: number,
  map: RuntimeMap,
) => {
  const worldWidth = map.worldBounds.xmax - map.worldBounds.xmin
  const worldHeight = map.worldBounds.zmax - map.worldBounds.zmin
  return {
    x: ((point.x - map.worldBounds.xmin) / worldWidth) * width,
    y: ((map.worldBounds.zmax - point.z) / worldHeight) * height,
  }
}

const projectRuntimeBounds = (
  bounds: NonNullable<RuntimeLayerRecord['boundsUnity']>,
  width: number,
  height: number,
  map: RuntimeMap,
) => {
  if (
    bounds.xmin === null || bounds.xmax === null ||
    bounds.zmin === null || bounds.zmax === null
  ) return null
  const topLeft = projectRuntimePoint({ x: bounds.xmin, z: bounds.zmax }, width, height, map)
  const bottomRight = projectRuntimePoint({ x: bounds.xmax, z: bounds.zmin }, width, height, map)
  return {
    x: Math.min(topLeft.x, bottomRight.x),
    y: Math.min(topLeft.y, bottomRight.y),
    width: Math.abs(bottomRight.x - topLeft.x),
    height: Math.abs(bottomRight.y - topLeft.y),
  }
}

function drawRuntimeLayers(
  parent: Container,
  width: number,
  height: number,
  map: RuntimeMap,
  mapId: MapId,
  layers: LayerState,
  data: RuntimeLayerData,
) {
  const layerRoot = parent
  layerRoot.label = 'stage119-runtime-layers'
  const accent = mapId === 'map01' ? 0xf0dd00 : 0x72e2b5
  const instances = data.instances?.records ?? []
  const roads = data.roads?.records ?? []
  const water = data.water?.records ?? []
  const effects = data.effects?.records ?? []
  const lights = data.lights?.records ?? []

  if (layers.instances && instances.length) {
    const points = new Graphics()
    instances.forEach((record, index) => {
      const position = record.positionUnity
      if (!position) return
      const point = projectRuntimePoint(position, width, height, map)
      const color = record.category === 'road' ? 0x2b3332 : accent
      points.circle(point.x, point.y, index % 7 === 0 ? 2.2 : 1.25).fill({ color, alpha: 0.35 })
    })
    layerRoot.addChild(points)
  }

  if (layers.instances && roads.length) {
    const roadLines = new Graphics()
    roads.forEach((record) => {
      const position = record.positionUnity
      if (!position) return
      const point = projectRuntimePoint(position, width, height, map)
      roadLines.moveTo(point.x - 3, point.y).lineTo(point.x + 3, point.y)
    })
    roadLines.stroke({ color: mapId === 'map01' ? 0x59615d : 0x2a765f, width: 1.05, alpha: 0.42 })
    layerRoot.addChild(roadLines)
  }

  if (layers.water && water.length) {
    const waterOverlay = new Graphics()
    water.forEach((record) => {
      const rect = record.geometry?.worldRectUnityXZ
        ? projectRuntimeBounds(record.geometry.worldRectUnityXZ, width, height, map)
        : record.boundsUnity
          ? projectRuntimeBounds(record.boundsUnity, width, height, map)
          : null
      if (rect) {
        waterOverlay.rect(rect.x, rect.y, Math.max(2, rect.width), Math.max(2, rect.height))
      } else if (record.positionUnity) {
        const point = projectRuntimePoint(record.positionUnity, width, height, map)
        waterOverlay.circle(point.x, point.y, 3)
      }
    })
    waterOverlay.fill({ color: mapId === 'map01' ? 0x5a9aa0 : 0x56c99c, alpha: 0.1 })
    waterOverlay.stroke({ color: mapId === 'map01' ? 0x5a9aa0 : 0x56c99c, width: 1.2, alpha: 0.52 })
    layerRoot.addChild(waterOverlay)
  }

  if (layers.effects && effects.length) {
    const effectOverlay = new Graphics()
    effects.forEach((record, index) => {
      if (!record.positionUnity) return
      const point = projectRuntimePoint(record.positionUnity, width, height, map)
      effectOverlay.circle(point.x, point.y, 2.5 + (index % 3))
    })
    effectOverlay.stroke({ color: accent, width: 1.1, alpha: 0.68 })
    layerRoot.addChild(effectOverlay)
  }

  if (layers.effects && lights.length) {
    const lightOverlay = new Graphics()
    lights.forEach((record) => {
      if (!record.positionUnity) return
      const point = projectRuntimePoint(record.positionUnity, width, height, map)
      lightOverlay.circle(point.x, point.y, 2.4).fill({ color: 0xffca78, alpha: 0.78 })
    })
    layerRoot.addChild(lightOverlay)
  }

}

function drawHalftone(
  parent: Container,
  width: number,
  height: number,
  mapId: MapId,
) {
  const field = new Graphics()
  const color = mapId === 'map01' ? 0x242a29 : 0x2c5a4c
  const spacing = 13

  for (let y = 18; y < height; y += spacing) {
    for (let x = 18; x < width; x += spacing) {
      const mask =
        mapId === 'map01'
          ? x < width * 0.28 && y > height * 0.58
          : x > width * 0.68 && y < height * 0.3
      if (mask && ((x + y) / spacing) % 3 !== 0) {
        field.circle(x, y, 0.8)
      }
    }
  }

  field.fill({ color, alpha: 0.18 })
  parent.addChild(field)
}

function drawValley(
  parent: Container,
  width: number,
  height: number,
  layers: LayerState,
  targets: AnimatedTargets,
) {
  if (layers.terrain) {
    polygon(
      parent,
      [
        0,
        height * 0.13,
        width * 0.18,
        height * 0.04,
        width * 0.41,
        height * 0.2,
        width * 0.56,
        height * 0.08,
        width * 0.78,
        height * 0.17,
        width,
        height * 0.11,
        width,
        0,
        0,
        0,
      ],
      0xdce1de,
    )
    polygon(
      parent,
      [
        0,
        height * 0.72,
        width * 0.17,
        height * 0.53,
        width * 0.34,
        height * 0.68,
        width * 0.5,
        height * 0.39,
        width * 0.72,
        height * 0.56,
        width,
        height * 0.31,
        width,
        height,
        0,
        height,
      ],
      0xc8d0cd,
    )
    polygon(
      parent,
      [
        width * 0.63,
        height,
        width * 0.78,
        height * 0.72,
        width,
        height * 0.61,
        width,
        height,
      ],
      0xb9c5c1,
      0.82,
    )
  }

  if (layers.water) {
    const river = new Graphics()
    river
      .moveTo(width * 0.66, -20)
      .bezierCurveTo(
        width * 0.61,
        height * 0.18,
        width * 0.73,
        height * 0.31,
        width * 0.67,
        height * 0.47,
      )
      .bezierCurveTo(
        width * 0.61,
        height * 0.64,
        width * 0.77,
        height * 0.79,
        width * 0.69,
        height + 30,
      )
      .stroke({ color: 0x56c4c8, width: Math.max(7, width * 0.008), alpha: 0.78 })
    parent.addChild(river)

    for (let index = 0; index < 12; index += 1) {
      const signal = dot(
        parent,
        width * 0.67 + Math.sin(index * 1.7) * 12,
        (height / 11) * index,
        1.4,
        0xeefcff,
        0.9,
      )
      targets.waterSignals.push(signal)
    }
  }

  if (layers.instances) {
    const roadShadow = new Graphics()
    roadShadow
      .moveTo(-40, height * 0.77)
      .bezierCurveTo(
        width * 0.22,
        height * 0.48,
        width * 0.53,
        height * 0.73,
        width + 40,
        height * 0.34,
      )
      .stroke({ color: 0x222928, width: Math.max(18, width * 0.021) })
    parent.addChild(roadShadow)

    const roadCore = new Graphics()
    roadCore
      .moveTo(-40, height * 0.77)
      .bezierCurveTo(
        width * 0.22,
        height * 0.48,
        width * 0.53,
        height * 0.73,
        width + 40,
        height * 0.34,
      )
      .stroke({ color: 0xf0dc00, width: Math.max(3, width * 0.004) })
    parent.addChild(roadCore)

    const structures = [
      [0.09, 0.2, 0.045, 0.018],
      [0.19, 0.17, 0.042, 0.025],
      [0.3, 0.33, 0.064, 0.037],
      [0.43, 0.47, 0.056, 0.025],
      [0.49, 0.22, 0.026, 0.039],
      [0.56, 0.58, 0.032, 0.027],
      [0.71, 0.64, 0.05, 0.035],
      [0.84, 0.15, 0.031, 0.031],
      [0.9, 0.09, 0.024, 0.039],
      [0.8, 0.78, 0.07, 0.029],
      [0.61, 0.81, 0.052, 0.041],
      [0.16, 0.44, 0.052, 0.025],
    ]

    structures.forEach(([x, y, w, h], index) => {
      const block = new Graphics()
      block
        .rect(width * x, height * y, width * w, height * h)
        .fill({ color: index % 4 === 0 ? 0x68706d : 0x252c2b })
      parent.addChild(block)
      line(
        parent,
        [
          width * x,
          height * (y + h + 0.009),
          width * (x + w * 0.66),
          height * (y + h + 0.009),
        ],
        0xe5d600,
        2,
        0.9,
      )
    })
  }

  if (layers.effects) {
    const markerData = [
      [0.21, 0.25, 0xf1dd00],
      [0.35, 0.31, 0xf25343],
      [0.48, 0.39, 0xe4d400],
      [0.55, 0.45, 0xe4d400],
      [0.69, 0.58, 0xf25343],
      [0.78, 0.53, 0xe4d400],
      [0.89, 0.73, 0xe4d400],
      [0.13, 0.64, 0x00bec2],
    ]
    markerData.forEach(([x, y, color], index) => {
      const pulse = new Graphics()
      pulse
        .circle(width * x, height * y, index % 3 === 0 ? 3.2 : 2.2)
        .fill({ color })
        .circle(width * x, height * y, 8)
        .stroke({ color, width: 1, alpha: 0.25 })
      parent.addChild(pulse)
      targets.pulseNodes.push(pulse)
    })
  }
}

function xirangField(
  parent: Container,
  originX: number,
  originY: number,
  columns: number,
  rows: number,
  cell: number,
  gapPattern: number,
  targets: AnimatedTargets,
  pale = false,
) {
  for (let row = 0; row < rows; row += 1) {
    for (let column = 0; column < columns; column += 1) {
      const shouldGap =
        (row * 7 + column * 3 + gapPattern) % 11 === 0 ||
        (row === Math.floor(rows / 2) && column % 5 === 2)
      if (shouldGap) continue

      const block = new Graphics()
      const x = originX + column * cell
      const y = originY + row * cell
      block
        .rect(x, y, cell - 2, cell - 2)
        .fill({
          color: pale ? 0xa8d5c0 : (row + column) % 5 === 0 ? 0x4b7668 : 0x6d9588,
          alpha: pale ? 0.88 : 0.9,
        })
        .stroke({ color: 0x335e51, width: 0.8, alpha: 0.58 })
      parent.addChild(block)
      targets.xirangBlocks.push(block)
    }
  }
}

function drawWuling(
  parent: Container,
  width: number,
  height: number,
  layers: LayerState,
  targets: AnimatedTargets,
) {
  if (layers.terrain) {
    polygon(
      parent,
      [
        0,
        0,
        width,
        0,
        width,
        height * 0.11,
        width * 0.76,
        height * 0.17,
        width * 0.59,
        height * 0.06,
        width * 0.37,
        height * 0.2,
        width * 0.16,
        height * 0.09,
        0,
        height * 0.22,
      ],
      0xdce9e2,
    )
    polygon(
      parent,
      [
        0,
        height * 0.67,
        width * 0.17,
        height * 0.5,
        width * 0.33,
        height * 0.68,
        width * 0.51,
        height * 0.4,
        width * 0.72,
        height * 0.58,
        width,
        height * 0.39,
        width,
        height,
        0,
        height,
      ],
      0xb7d3c7,
    )

    const contours = new Graphics()
    for (let index = 0; index < 7; index += 1) {
      contours
        .moveTo(width * (0.04 + index * 0.012), height * (0.8 + index * 0.018))
        .bezierCurveTo(
          width * 0.22,
          height * (0.65 + index * 0.014),
          width * 0.35,
          height * (0.92 - index * 0.012),
          width * 0.47,
          height * (0.73 + index * 0.012),
        )
    }
    contours.stroke({ color: 0x3d7966, width: 1, alpha: 0.2 })
    parent.addChild(contours)
  }

  if (layers.water) {
    const water = new Graphics()
    water
      .moveTo(width * 0.54, -30)
      .bezierCurveTo(
        width * 0.5,
        height * 0.23,
        width * 0.63,
        height * 0.28,
        width * 0.53,
        height * 0.53,
      )
      .bezierCurveTo(
        width * 0.47,
        height * 0.67,
        width * 0.6,
        height * 0.77,
        width * 0.48,
        height + 40,
      )
      .stroke({ color: 0x4abdaf, width: Math.max(11, width * 0.012), alpha: 0.8 })
    parent.addChild(water)

    const waterCore = new Graphics()
    waterCore
      .moveTo(width * 0.54, -30)
      .bezierCurveTo(
        width * 0.5,
        height * 0.23,
        width * 0.63,
        height * 0.28,
        width * 0.53,
        height * 0.53,
      )
      .bezierCurveTo(
        width * 0.47,
        height * 0.67,
        width * 0.6,
        height * 0.77,
        width * 0.48,
        height + 40,
      )
      .stroke({ color: 0xd9fff0, width: 2, alpha: 0.7 })
    parent.addChild(waterCore)
  }

  if (layers.instances) {
    const monolith = new Graphics()
    monolith
      .rect(width * 0.42, height * 0.1, width * 0.16, height * 0.61)
      .fill({ color: 0x21372f })
      .rect(width * 0.495, height * 0.08, 3, height * 0.66)
      .fill({ color: 0x73e7b7 })
    parent.addChild(monolith)

    const cell = Math.max(13, Math.min(23, width * 0.021))
    xirangField(
      parent,
      width * 0.08,
      height * 0.2,
      10,
      6,
      cell,
      2,
      targets,
    )
    xirangField(
      parent,
      width * 0.69,
      height * 0.14,
      9,
      7,
      cell,
      5,
      targets,
    )
    xirangField(
      parent,
      width * 0.27,
      height * 0.7,
      15,
      4,
      cell,
      7,
      targets,
      true,
    )
    xirangField(
      parent,
      width * 0.505,
      height * 0.37,
      6,
      5,
      cell,
      4,
      targets,
      true,
    )

    const garden = new Graphics()
    for (let index = 0; index < 18; index += 1) {
      const angle = (Math.PI * 2 * index) / 18
      const radius = width * (0.024 + (index % 3) * 0.007)
      garden.circle(
        width * 0.18 + Math.cos(angle) * radius,
        height * 0.78 + Math.sin(angle) * radius,
        2 + (index % 2),
      )
    }
    garden.fill({ color: 0x2e7c62, alpha: 0.68 })
    parent.addChild(garden)
  }

  if (layers.effects) {
    const markerData = [
      [0.12, 0.22, 0x68ddb0],
      [0.18, 0.18, 0x68ddb0],
      [0.29, 0.31, 0xf05243],
      [0.48, 0.47, 0xe8e40f],
      [0.67, 0.39, 0x68ddb0],
      [0.84, 0.16, 0x68ddb0],
      [0.78, 0.74, 0xf05243],
      [0.42, 0.86, 0x00bec2],
    ]
    markerData.forEach(([x, y, color], index) => {
      const pulse = new Graphics()
      pulse
        .circle(width * x, height * y, index % 3 === 0 ? 3.3 : 2.2)
        .fill({ color })
        .circle(width * x, height * y, 9)
        .stroke({ color, width: 1, alpha: 0.28 })
      parent.addChild(pulse)
      targets.pulseNodes.push(pulse)
    })
  }
}

function drawAxisLabels(
  parent: Container,
  width: number,
  height: number,
  mapId: MapId,
) {
  const style = new TextStyle({
    fontFamily: 'Cascadia Mono, Consolas, monospace',
    fontSize: 9,
    fill: mapId === 'map01' ? 0x66706d : 0x5d766d,
    letterSpacing: 1,
  })
  const labels = [
    ['X-', 10, height * 0.5],
    ['X+', width - 22, height * 0.5],
    ['Z+', width * 0.5, 13],
    ['Z-', width * 0.5, height - 18],
  ] as const
  labels.forEach(([text, x, y]) => {
    const label = new Text({ text, style })
    label.position.set(x, y)
    parent.addChild(label)
  })
}


function drawOverviewLoading(
  parent: Container,
  width: number,
  height: number,
  mapId: MapId,
) {
  const accent = mapId === 'map01' ? 0xf0dd00 : 0x70dfb2
  const ink = mapId === 'map01' ? 0x34413e : 0x28564b
  const plate = new Graphics()
  plate.rect(0, 0, width, height).fill({ color: mapId === 'map01' ? 0x27312e : 0x173a33, alpha: 0.92 })
  plate.rect(0, 0, width, height).stroke({ color: accent, width: 1, alpha: 0.22 })
  parent.addChild(plate)

  const calibration = new Graphics()
  for (let index = 1; index < 9; index += 1) {
    const offset = (width / 10) * index
    calibration.moveTo(offset, 0).lineTo(offset - width * 0.12, height)
  }
  for (let index = 1; index < 7; index += 1) {
    const offset = (height / 8) * index
    calibration.moveTo(0, offset).lineTo(width, offset + height * 0.04)
  }
  calibration.stroke({ color: ink, width: 1, alpha: 0.42 })
  parent.addChild(calibration)

  const reticle = new Graphics()
  reticle
    .circle(width * 0.5, height * 0.5, Math.min(width, height) * 0.16)
    .stroke({ color: accent, width: 1, alpha: 0.48 })
    .moveTo(width * 0.5 - 22, height * 0.5)
    .lineTo(width * 0.5 + 22, height * 0.5)
    .moveTo(width * 0.5, height * 0.5 - 22)
    .lineTo(width * 0.5, height * 0.5 + 22)
  reticle.stroke({ color: accent, width: 1, alpha: 0.62 })
  parent.addChild(reticle)

  const corner = new Graphics()
  corner
    .moveTo(14, 28).lineTo(14, 14).lineTo(28, 14)
    .moveTo(width - 28, height - 14).lineTo(width - 14, height - 14).lineTo(width - 14, height - 28)
  corner.stroke({ color: accent, width: 1.4, alpha: 0.7 })
  parent.addChild(corner)
}

export function MapViewport({
  mapId,
  layers,
  chunkMode,
  selection,
  overviewRevision,
  onSelectionChange,
  onOverviewSourceChange,
}: Props) {
  const hostRef = useRef<HTMLDivElement>(null)
  const appRef = useRef<Application | null>(null)
  const worldLayerRef = useRef<Container | null>(null)
  const runtimeLayerContainerRef = useRef<Container | null>(null)
  const rawSelectionRef = useRef<Graphics | null>(null)
  const canonicalSelectionRef = useRef<Graphics | null>(null)
  const mapViewportRef = useRef<ScreenViewport>({ x: 0, y: 0, width: 1, height: 1 })
  const renderSceneRef = useRef<(() => void) | null>(null)
  const drawSelectionRef = useRef<((selection: ChunkSelection | null) => void) | null>(null)
  const layersRef = useRef(layers)
  const selectionRef = useRef(selection)
  const chunkModeRef = useRef(chunkMode)
  const runtimeLayerDataRef = useRef<RuntimeLayerData>({})
  const zoomRef = useRef(1)
  const panRef = useRef({ x: 0, y: 0 })
  const cameraByMapRef = useRef<Record<MapId, { zoom: number; x: number; y: number }>>({
    map01: { zoom: 1, x: 0, y: 0 },
    map02: { zoom: 1, x: 0, y: 0 },
  })
  const applyCameraRef = useRef<(() => void) | null>(null)
  const zoomAtRef = useRef<((next: number, anchor?: { x: number; y: number }) => void) | null>(null)
  const resetCameraRef = useRef<(() => void) | null>(null)
  const [zoom, setZoom] = useState(1)
  const [isPanning, setIsPanning] = useState(false)
  const [overviewState, setOverviewState] = useState<'loading' | 'dev-override' | 'runtime-config' | 'fallback'>('loading')
  const [runtimeLayerState, setRuntimeLayerState] = useState<'loading' | 'ready' | 'partial' | 'offline'>('loading')
  const overviewPhaseRef = useRef<'loading' | 'loaded' | 'fallback'>('loading')
  const overviewRevealRef = useRef(0)
  const map = MAPS[mapId]
  const runtimeMap = getRuntimeMap(mapId)
  const canonicalBounds = selection?.canonicalBounds ?? null
  const mappedSectorCount = selection?.sectors.filter((sector) => sector.coverage === 'mapped').length ?? 0
  const selectionSectorKey = (selection?.sectors ?? []).map((sector) => sector.key).join(',')
  const lightScanStatus = runtimeLayerDataRef.current.lights?.meta.scanStatus ?? 'loading'
  const waterCandidateCount = runtimeLayerDataRef.current.water?.meta.bindingCounts?.candidate ?? 0

  useEffect(() => {
    const camera = cameraByMapRef.current[mapId]
    zoomRef.current = camera.zoom
    panRef.current = { x: camera.x, y: camera.y }
    setZoom(camera.zoom)
    overviewPhaseRef.current = 'loading'
    overviewRevealRef.current = 0
    setOverviewState('loading')
    onOverviewSourceChange?.('loading')
  }, [mapId, overviewRevision, onOverviewSourceChange])

  useEffect(() => {
    layersRef.current = layers
    renderSceneRef.current?.()
  }, [layers])

  useEffect(() => {
    chunkModeRef.current = chunkMode
  }, [chunkMode])

  useEffect(() => {
    const controller = new AbortController()
    const sectorKeys = (selection?.sectors ?? []).map((sector) => sector.key).filter(Boolean)
    const layersToLoad: RuntimeLayerId[] = ['instances', 'roads', 'water', 'effects', 'lights']
    setRuntimeLayerState('loading')
    void Promise.allSettled(
      layersToLoad.map((layer) => fetchRuntimeLayer({
        mapId,
        layer,
        sectorKeys,
        limit: layer === 'instances' ? 2400 : 900,
        signal: controller.signal,
      })),
    ).then((results) => {
      if (controller.signal.aborted) return
      const nextData: RuntimeLayerData = {}
      let successCount = 0
      results.forEach((result, index) => {
        if (result.status === 'fulfilled') {
          nextData[layersToLoad[index]] = result.value
          successCount += 1
        }
      })
      runtimeLayerDataRef.current = nextData
      setRuntimeLayerState(successCount === layersToLoad.length ? 'ready' : successCount ? 'partial' : 'offline')
      renderSceneRef.current?.()
    })
    return () => controller.abort()
  }, [mapId, selectionSectorKey])

  useEffect(() => {
    selectionRef.current = selection
    drawSelectionRef.current?.(selection)
  }, [selection])

  useEffect(() => {
    zoomRef.current = zoom
    cameraByMapRef.current[mapId] = { zoom, x: panRef.current.x, y: panRef.current.y }
    applyCameraRef.current?.()
  }, [mapId, zoom])

  useEffect(() => {
    const host = hostRef.current
    if (!host) return

    let disposed = false
    let app: Application | null = null
    let overviewTexture: Texture | null = null
    let resizeObserver: ResizeObserver | null = null
    let detachCanvasInteractions: (() => void) | null = null
    let dragStart: { x: number; y: number } | null = null
    let panStart: { pointerX: number; pointerY: number; x: number; y: number } | null = null
    let spacePressed = false
    let zoomFrame = 0
    overviewPhaseRef.current = 'loading'
    overviewRevealRef.current = 0
    let overviewReadyNotified = false
    let loadedOverviewSource: 'dev-override' | 'runtime-config' = 'runtime-config'

    const targets: AnimatedTargets = {
      scanner: null,
      pulseNodes: [],
      xirangBlocks: [],
      waterSignals: [],
      overviewSprite: null,
    }

    const localPoint = (event: FederatedPointerEvent) => {
      if (!app) return { x: 0, y: 0 }
      const width = app.screen.width
      const height = app.screen.height
      return {
        x: clamp((event.global.x - width / 2 - panRef.current.x) / zoomRef.current + width / 2, 0, width),
        y: clamp((event.global.y - height / 2 - panRef.current.y) / zoomRef.current + height / 2, 0, height),
      }
    }

    const constrainPan = (x: number, y: number, scale = zoomRef.current) => {
      const viewport = mapViewportRef.current
      const maxX = Math.max(0, (viewport.width * scale - viewport.width) / 2)
      const maxY = Math.max(0, (viewport.height * scale - viewport.height) / 2)
      return {
        x: clamp(x, -maxX, maxX),
        y: clamp(y, -maxY, maxY),
      }
    }

    const publishZoom = () => {
      if (zoomFrame) return
      zoomFrame = window.requestAnimationFrame(() => {
        zoomFrame = 0
        setZoom(zoomRef.current)
      })
    }

    const applyCamera = () => {
      if (!app || !worldLayerRef.current) return
      const constrained = constrainPan(panRef.current.x, panRef.current.y)
      panRef.current = constrained
      const world = worldLayerRef.current
      world.position.set(app.screen.width / 2 + constrained.x, app.screen.height / 2 + constrained.y)
      world.pivot.set(app.screen.width / 2, app.screen.height / 2)
      world.scale.set(zoomRef.current)
      cameraByMapRef.current[mapId] = { zoom: zoomRef.current, ...constrained }
    }

    applyCameraRef.current = applyCamera
    zoomAtRef.current = (next, anchor) => {
      if (!app) return
      const previous = zoomRef.current
      const value = clamp(next, 0.8, 5.5)
      const point = anchor ?? { x: app.screen.width / 2, y: app.screen.height / 2 }
      if (Math.abs(value - previous) < 0.0001) return
      const centerX = app.screen.width / 2
      const centerY = app.screen.height / 2
      const ratio = value / previous
      panRef.current = constrainPan(
        point.x - centerX - (point.x - centerX - panRef.current.x) * ratio,
        point.y - centerY - (point.y - centerY - panRef.current.y) * ratio,
        value,
      )
      zoomRef.current = value
      applyCamera()
      publishZoom()
    }
    resetCameraRef.current = () => {
      zoomRef.current = 1
      panRef.current = { x: 0, y: 0 }
      applyCamera()
      publishZoom()
    }

    const drawCanonicalSelection = (nextSelection: ChunkSelection | null) => {
      if (!app || !canonicalSelectionRef.current || !rawSelectionRef.current) return
      rawSelectionRef.current.clear()
      canonicalSelectionRef.current.clear()
      if (!nextSelection) return

      const viewport = mapViewportRef.current
      const width = viewport.width
      const height = viewport.height
      const worldWidth = runtimeMap.worldBounds.xmax - runtimeMap.worldBounds.xmin
      const worldHeight = runtimeMap.worldBounds.zmax - runtimeMap.worldBounds.zmin
      const bounds = nextSelection.canonicalBounds
      const canonicalX =
        ((bounds.xmin - runtimeMap.worldBounds.xmin) / worldWidth) * width
      const canonicalY =
        ((runtimeMap.worldBounds.zmax - bounds.zmax) / worldHeight) * height
      const canonicalWidth = ((bounds.xmax - bounds.xmin) / worldWidth) * width
      const canonicalHeight = ((bounds.zmax - bounds.zmin) / worldHeight) * height
      const accent = mapId === 'map01' ? 0xf0dd00 : 0x6fe0b2

      canonicalSelectionRef.current
        .rect(canonicalX, canonicalY, canonicalWidth, canonicalHeight)
        .fill({ color: accent, alpha: 0.12 })
        .stroke({ color: accent, width: 2.2, alpha: 1 })
        .moveTo(canonicalX + 8, canonicalY)
        .lineTo(canonicalX, canonicalY)
        .lineTo(canonicalX, canonicalY + 8)
        .moveTo(canonicalX + canonicalWidth - 8, canonicalY)
        .lineTo(canonicalX + canonicalWidth, canonicalY)
        .lineTo(canonicalX + canonicalWidth, canonicalY + 8)
        .moveTo(canonicalX, canonicalY + canonicalHeight - 8)
        .lineTo(canonicalX, canonicalY + canonicalHeight)
        .lineTo(canonicalX + 8, canonicalY + canonicalHeight)
        .moveTo(canonicalX + canonicalWidth - 8, canonicalY + canonicalHeight)
        .lineTo(canonicalX + canonicalWidth, canonicalY + canonicalHeight)
        .lineTo(canonicalX + canonicalWidth, canonicalY + canonicalHeight - 8)
        .stroke({ color: 0x202725, width: 4, alpha: 0.9 })
    }

    drawSelectionRef.current = drawCanonicalSelection

    const redrawRawSelection = (
      start: { x: number; y: number },
      end: { x: number; y: number },
    ) => {
      if (!rawSelectionRef.current) return
      const viewport = mapViewportRef.current
      const startX = clamp(start.x, viewport.x, viewport.x + viewport.width) - viewport.x
      const endX = clamp(end.x, viewport.x, viewport.x + viewport.width) - viewport.x
      const startY = clamp(start.y, viewport.y, viewport.y + viewport.height) - viewport.y
      const endY = clamp(end.y, viewport.y, viewport.y + viewport.height) - viewport.y
      const rawX = Math.min(startX, endX)
      const rawY = Math.min(startY, endY)
      const rawWidth = Math.abs(endX - startX)
      const rawHeight = Math.abs(endY - startY)

      rawSelectionRef.current
        .clear()
        .rect(rawX, rawY, rawWidth, rawHeight)
        .fill({ color: 0xffffff, alpha: 0.08 })
        .stroke({ color: 0xffffff, width: 1, alpha: 0.68 })
    }

    const drawScene = () => {
      if (!app || disposed) return
      const width = Math.max(1, host.clientWidth)
      const height = Math.max(1, host.clientHeight)
      app.renderer.resize(width, height)
      app.stage.hitArea = new Rectangle(0, 0, width, height)
      const persistentRuntimeLayer = runtimeLayerContainerRef.current
      persistentRuntimeLayer?.removeFromParent()
      app.stage.removeChildren().forEach((child) => child.destroy({ children: true }))
      const mapSize = Math.max(1, Math.min(width, height))
      const mapViewport = {
        x: (width - mapSize) / 2,
        y: (height - mapSize) / 2,
        width: mapSize,
        height: mapSize,
      }
      mapViewportRef.current = mapViewport

      targets.scanner = null
      targets.pulseNodes = []
      targets.xirangBlocks = []
      targets.waterSignals = []
      targets.overviewSprite = null

      const backdrop = new Graphics()
      backdrop
        .rect(0, 0, width, height)
        .fill({ color: mapId === 'map01' ? 0xe8ece9 : 0xe7f0eb })
      app.stage.addChild(backdrop)

      const world = new Container()
      worldLayerRef.current = world
      app.stage.addChild(world)
      applyCamera()

      const mapContent = new Container()
      mapContent.position.set(mapViewport.x, mapViewport.y)
      world.addChild(mapContent)

      const mapBackdrop = new Graphics()
      mapBackdrop
        .rect(0, 0, mapViewport.width, mapViewport.height)
        .fill({ color: mapId === 'map01' ? 0xe8ece9 : 0xe7f0eb })
        .stroke({ color: mapId === 'map01' ? 0x66706d : 0x5d766d, width: 1, alpha: 0.28 })
      mapContent.addChild(mapBackdrop)

      const overviewPhase = overviewPhaseRef.current
      if (overviewTexture && overviewPhase === 'loaded' && layersRef.current.terrain) {
        const overview = new Sprite(overviewTexture)
        overview.width = mapViewport.width
        overview.height = mapViewport.height
        overview.alpha = overviewRevealRef.current
        targets.overviewSprite = overview
        mapContent.addChild(overview)
      } else if (overviewPhase === 'fallback') {
        drawHalftone(mapContent, mapViewport.width, mapViewport.height, mapId)
        if (mapId === 'map01') {
          drawValley(mapContent, mapViewport.width, mapViewport.height, layersRef.current, targets)
        } else {
          drawWuling(mapContent, mapViewport.width, mapViewport.height, layersRef.current, targets)
        }
      } else if (overviewPhase === 'loading') {
        drawOverviewLoading(mapContent, mapViewport.width, mapViewport.height, mapId)
      }

      if (layersRef.current.instances && overviewPhase !== 'loading') {
        drawRuntimeOwnership(
          mapContent,
          mapViewport.width,
          mapViewport.height,
          runtimeMap,
          mapId,
        )
      }
      if (overviewPhase !== 'loading') {
        const runtimeLayerContainer = persistentRuntimeLayer ?? new Container()
        runtimeLayerContainer.removeChildren().forEach((child) => child.destroy({ children: true }))
        runtimeLayerContainerRef.current = runtimeLayerContainer
        drawRuntimeLayers(
          runtimeLayerContainer,
          mapViewport.width,
          mapViewport.height,
          runtimeMap,
          mapId,
          layersRef.current,
          runtimeLayerDataRef.current,
        )
        mapContent.addChild(runtimeLayerContainer)
      }
      drawGrid(mapContent, mapViewport.width, mapViewport.height, runtimeMap, mapId)
      drawAxisLabels(mapContent, mapViewport.width, mapViewport.height, mapId)

      const scanner = new Graphics()
      if (overviewPhase === 'loading' || layersRef.current.effects) {
        if (mapId === 'map01') {
          scanner
            .rect(0, 0, mapViewport.width, 1)
            .fill({ color: 0xf0dd00, alpha: 0.82 })
            .rect(0, 2, mapViewport.width, 12)
            .fill({ color: 0xf0dd00, alpha: 0.035 })
        } else {
          scanner
            .rect(0, 0, 1, mapViewport.height)
            .fill({ color: 0x72e2b5, alpha: 0.86 })
            .rect(2, 0, 18, mapViewport.height)
            .fill({ color: 0x72e2b5, alpha: 0.04 })
        }
        mapContent.addChild(scanner)
        targets.scanner = scanner
      }

      const rawSelection = new Graphics()
      const canonicalSelection = new Graphics()
      mapContent.addChild(rawSelection)
      mapContent.addChild(canonicalSelection)
      rawSelectionRef.current = rawSelection
      canonicalSelectionRef.current = canonicalSelection
      drawCanonicalSelection(selectionRef.current)
    }

    renderSceneRef.current = drawScene

    const boot = async () => {
      const next = new Application()
      await next.init({
        width: Math.max(1, host.clientWidth),
        height: Math.max(1, host.clientHeight),
        antialias: true,
        autoDensity: true,
        backgroundAlpha: 0,
        resolution: Math.min(window.devicePixelRatio || 1, 2),
      })
      if (disposed) {
        next.destroy(true)
        return
      }

      app = next
      appRef.current = next
      host.replaceChildren(next.canvas)
      next.stage.eventMode = 'static'
      next.stage.cursor = 'crosshair'

      next.stage.on('pointerdown', (event: FederatedPointerEvent) => {
        const wantsPan = event.button === 1 || (event.button === 0 && spacePressed)
        if (wantsPan) {
          panStart = {
            pointerX: event.global.x,
            pointerY: event.global.y,
            x: panRef.current.x,
            y: panRef.current.y,
          }
          dragStart = null
          rawSelectionRef.current?.clear()
          next.stage.cursor = 'grabbing'
          setIsPanning(true)
          return
        }
        if (event.button !== 0) return
        dragStart = localPoint(event)
        redrawRawSelection(dragStart, dragStart)
      })
      next.stage.on('pointermove', (event: FederatedPointerEvent) => {
        if (panStart) {
          panRef.current = constrainPan(
            panStart.x + event.global.x - panStart.pointerX,
            panStart.y + event.global.y - panStart.pointerY,
          )
          applyCamera()
          return
        }
        if (!dragStart) return
        redrawRawSelection(dragStart, localPoint(event))
      })
      const finish = (event: FederatedPointerEvent) => {
        if (panStart) {
          panStart = null
          next.stage.cursor = spacePressed ? 'grab' : 'crosshair'
          setIsPanning(false)
          return
        }
        if (!dragStart || !app) return
        const end = localPoint(event)
        if (Math.abs(end.x - dragStart.x) >= 2 && Math.abs(end.y - dragStart.y) >= 2) {
          const requestedBounds = screenDragToWorldBounds(
            runtimeMap,
            dragStart,
            end,
            mapViewportRef.current,
          )
          const resolved = resolveSelection(
            REGION_MAP_RUNTIME_MANIFEST,
            mapId,
            requestedBounds,
            chunkModeRef.current,
          )
          selectionRef.current = resolved
          drawCanonicalSelection(resolved)
          onSelectionChange(resolved)
        } else {
          rawSelectionRef.current?.clear()
        }
        dragStart = null
      }
      next.stage.on('pointerup', finish)
      next.stage.on('pointerupoutside', finish)

      const handleWheel = (event: WheelEvent) => {
        event.preventDefault()
        const bounds = next.canvas.getBoundingClientRect()
        const anchor = {
          x: event.clientX - bounds.left,
          y: event.clientY - bounds.top,
        }
        const factor = Math.exp(-event.deltaY * 0.00125)
        zoomAtRef.current?.(zoomRef.current * factor, anchor)
      }
      const handleDoubleClick = (event: MouseEvent) => {
        event.preventDefault()
        resetCameraRef.current?.()
      }
      const preventMiddleMouseDefault = (event: MouseEvent) => {
        if (event.button !== 1) return
        event.preventDefault()
      }
      const handleKeyDown = (event: KeyboardEvent) => {
        if (event.code !== 'Space' || event.repeat) return
        const target = event.target as HTMLElement | null
        if (target?.matches('input, textarea, select, button, summary')) return
        event.preventDefault()
        spacePressed = true
        next.stage.cursor = panStart ? 'grabbing' : 'grab'
      }
      const handleKeyUp = (event: KeyboardEvent) => {
        if (event.code !== 'Space') return
        spacePressed = false
        if (!panStart) next.stage.cursor = 'crosshair'
      }
      next.canvas.addEventListener('wheel', handleWheel, { passive: false })
      next.canvas.addEventListener('dblclick', handleDoubleClick)
      next.canvas.addEventListener('pointerdown', preventMiddleMouseDefault)
      next.canvas.addEventListener('mousedown', preventMiddleMouseDefault)
      next.canvas.addEventListener('auxclick', preventMiddleMouseDefault)
      window.addEventListener('keydown', handleKeyDown)
      window.addEventListener('keyup', handleKeyUp)
      detachCanvasInteractions = () => {
        next.canvas.removeEventListener('wheel', handleWheel)
        next.canvas.removeEventListener('dblclick', handleDoubleClick)
        next.canvas.removeEventListener('pointerdown', preventMiddleMouseDefault)
        next.canvas.removeEventListener('mousedown', preventMiddleMouseDefault)
        next.canvas.removeEventListener('auxclick', preventMiddleMouseDefault)
        window.removeEventListener('keydown', handleKeyDown)
        window.removeEventListener('keyup', handleKeyUp)
      }

      let elapsed = 0
      next.ticker.add((ticker) => {
        elapsed += ticker.deltaMS
        if (targets.overviewSprite && overviewPhaseRef.current === 'loaded') {
          const revealProgress = Math.min(1, overviewRevealRef.current + ticker.deltaMS / 280)
          overviewRevealRef.current = revealProgress
          const easedReveal = 1 - Math.pow(1 - revealProgress, 3)
          targets.overviewSprite.alpha = easedReveal
          if (revealProgress >= 1 && !overviewReadyNotified) {
            overviewReadyNotified = true
            onOverviewSourceChange?.(loadedOverviewSource)
          }
        }
        if (targets.scanner && app) {
          if (mapId === 'map01') {
            targets.scanner.y =
              (elapsed * 0.055) % mapViewportRef.current.height
          } else {
            targets.scanner.x =
              (elapsed * 0.042) % mapViewportRef.current.width
          }
        }
        targets.pulseNodes.forEach((node, index) => {
          node.alpha = 0.58 + Math.sin(elapsed * 0.003 + index) * 0.35
          node.scale.set(0.96 + Math.sin(elapsed * 0.0024 + index) * 0.06)
        })
        targets.xirangBlocks.forEach((block, index) => {
          block.alpha =
            0.74 + Math.sin(elapsed * 0.0018 + index * 0.19) * 0.2
        })
        targets.waterSignals.forEach((signal, index) => {
          signal.y += 0.13 + (index % 3) * 0.02
          if (signal.y > mapViewportRef.current.height + 4) signal.y = -4
        })
      })

      drawScene()
      resizeObserver = new ResizeObserver(drawScene)
      resizeObserver.observe(host)

      const overviewUrl = resolveRuntimeAssetUrl(runtimeMap.overview.clean.apiUrl)
      if (!shouldRequestRuntimeOverview) {
        overviewPhaseRef.current = 'fallback'
        setOverviewState('fallback')
        onOverviewSourceChange?.('fallback')
        drawScene()
        return
      }
      try {
        const response = await fetch(overviewUrl, {
          cache: 'no-store',
          headers: { Accept: 'image/png,image/*' },
        })
        const contentType = response.headers.get('content-type') ?? ''
        if (!response.ok || !contentType.startsWith('image/')) {
          throw new Error(`Overview endpoint returned ${response.status} ${contentType}`)
        }
        const imageBitmap = await createImageBitmap(await response.blob())
        const texture = Texture.from(imageBitmap)
        if (disposed) {
          texture.destroy(true)
          return
        }
        overviewTexture = texture
        overviewPhaseRef.current = 'loaded'
        const sourceHeader = response.headers.get('X-Endfield-Map-Source')
        const source = sourceHeader === 'dev-override' || sourceHeader === 'runtime-config' ? sourceHeader : null
        if (!source) throw new Error(`Unsupported overview source header: ${sourceHeader ?? '(missing)'}`)
        loadedOverviewSource = source
        overviewRevealRef.current = 0
        overviewReadyNotified = false
        setOverviewState(source)
        drawScene()
      } catch {
        if (disposed) return
        overviewPhaseRef.current = 'fallback'
        setOverviewState('fallback')
        onOverviewSourceChange?.('fallback')
        drawScene()
      }
    }

    void boot()

    return () => {
      disposed = true
      detachCanvasInteractions?.()
      resizeObserver?.disconnect()
      renderSceneRef.current = null
      drawSelectionRef.current = null
      appRef.current = null
      worldLayerRef.current = null
      runtimeLayerContainerRef.current = null
      rawSelectionRef.current = null
      canonicalSelectionRef.current = null
      if (app) {
        app.destroy(true, { children: true })
      }
      overviewTexture?.destroy(true)
      if (zoomFrame) window.cancelAnimationFrame(zoomFrame)
      applyCameraRef.current = null
      zoomAtRef.current = null
      resetCameraRef.current = null
    }
  }, [mapId, onOverviewSourceChange, onSelectionChange, overviewRevision, runtimeMap])

  const updateZoom = (next: number) => {
    zoomAtRef.current?.(next)
  }

  const clearSelection = () => {
    rawSelectionRef.current?.clear()
    canonicalSelectionRef.current?.clear()
    onSelectionChange(null)
  }

  return (
    <div className="map-viewport" data-testid="map-viewport">
      <div ref={hostRef} className={`pixi-host ${isPanning ? 'is-panning' : ''}`} aria-label={`${map.title} 上帝视角地图`} />
      <div className="map-noise" aria-hidden="true" />
      <div className="map-register register-nw" aria-hidden="true" />
      <div className="map-register register-se" aria-hidden="true" />

      <div className="map-hud hud-top">
        <div className="map-identity">
          <span>{map.code}</span>
          <strong>{map.chineseName} / ORTHOGRAPHIC</strong>
          <small>{map.direction}</small>
        </div>
        <span className={`evidence-chip ${overviewState === 'dev-override' || overviewState === 'runtime-config' ? 'stable' : map.warningTone}`}>
          {overviewState === 'dev-override'
            ? 'DEV LOCAL OVERVIEW'
            : overviewState === 'runtime-config'
              ? 'ACTIVE RUNTIME / READY'
              : overviewState === 'loading'
                ? 'OVERVIEW / CONNECTING'
              : 'FALLBACK / API REQUIRED'}
        </span>
        <span className={`evidence-chip runtime-chip runtime-${runtimeLayerState}`}>
          {runtimeLayerState === 'ready'
            ? 'STAGE119 / LAYERS READY'
            : runtimeLayerState === 'partial'
              ? 'STAGE119 / PARTIAL'
              : runtimeLayerState === 'loading'
                ? 'STAGE119 / SYNCING'
                : 'STAGE119 / OFFLINE'}
        </span>
      </div>

      <div className="map-hud hud-bottom">
        <div className="selection-lockup">
          <span>SELECTION / CANONICAL</span>
          <strong>
            {canonicalBounds
              ? `X ${canonicalBounds.xmin}:${canonicalBounds.xmax} / Z ${canonicalBounds.zmin}:${canonicalBounds.zmax}`
              : 'DRAG TO ACQUIRE REGION'}
          </strong>
        </div>
        <div className="map-mode-chips">
          <span>SECTOR 128</span>
          <span>HALF-OPEN</span>
          {selection && <span>{mappedSectorCount}/{selection.sectors.length} MAPPED</span>}
          {runtimeLayerState !== 'loading' && <span>WATER CANDIDATE {waterCandidateCount}</span>}
          {runtimeLayerState !== 'loading' && <span>LIGHTS {lightScanStatus.replaceAll('_', ' ').toUpperCase()}</span>}
          <span>{Math.round(zoom * 100)}%</span>
        </div>
      </div>

      <div className="zoom-stack" aria-label="地图缩放">
        <button
          type="button"
          aria-label="放大地图"
          onClick={() => updateZoom(zoom + 0.25)}
          disabled={zoom >= 5.5}
        >
          <Plus size={15} />
        </button>
        <button
          type="button"
          aria-label="缩小地图"
          onClick={() => updateZoom(zoom - 0.25)}
          disabled={zoom <= 0.8}
        >
          <Minus size={15} />
        </button>
        <button
          type="button"
          aria-label="适应视口"
          onClick={() => resetCameraRef.current?.()}
        >
          <Maximize2 size={14} />
        </button>
        <button
          type="button"
          aria-label="清除选区"
          onClick={clearSelection}
          disabled={!selection}
        >
          <Crosshair size={14} />
        </button>
      </div>
      <div className="map-interaction-guide" aria-hidden="true">
        <span>WHEEL / ZOOM</span>
        <span>SPACE + DRAG / PAN</span>
        <span>DRAG / SELECT</span>
      </div>

      <AnimatePresence mode="wait">
        <motion.div
          key={mapId}
          className="theme-seal"
          initial={{ opacity: 0, x: 18 }}
          animate={{ opacity: 1, x: 0 }}
          exit={{ opacity: 0, x: -12 }}
          transition={{ duration: 0.34, ease: [0.22, 1, 0.36, 1] }}
        >
          <span>{mapId === 'map01' ? 'IV' : '武陵'}</span>
          <small>{map.tagline}</small>
        </motion.div>
      </AnimatePresence>
    </div>
  )
}
