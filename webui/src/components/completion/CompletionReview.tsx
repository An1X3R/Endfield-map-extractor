import { useState } from 'react'
import type { BlenderBuildResult, ExportMode, MapId } from '../../types'
import { ExtractionCompleteOverlay } from '../ExtractionCompleteOverlay'

function sampleBuild(mapId: MapId): BlenderBuildResult {
  const region = mapId === 'map02' ? 'wuling' : 'valley_iv'
  return {
    status: 'partial',
    pending: 12,
    scenes: [{
      path: `review_sample/${region}/scene_01.blend`,
      audit: `review_sample/${region}/audit_01.json`,
      plan: `review_sample/${region}/plan_01.json`,
      bytes: 1638400,
      sha256: 'review-sample',
      instances: 284,
      pending: 12,
    }],
  }
}

function reviewMapFromQuery(): MapId {
  const mapId = new URLSearchParams(window.location.search).get('map')
  if (mapId === null) return 'map02'
  if (mapId === 'map01' || mapId === 'map02') return mapId
  throw new Error(`Unknown completion review map: ${mapId}`)
}

export function CompletionReview() {
  const [visible, setVisible] = useState(true)
  const [mapId, setMapId] = useState<MapId>(reviewMapFromQuery)
  const [exportMode, setExportMode] = useState<ExportMode>('blend')
  const [run, setRun] = useState(0)

  const show = (nextMap: MapId, nextMode: ExportMode): void => {
    setMapId(nextMap)
    setExportMode(nextMode)
    setRun((value) => value + 1)
    setVisible(true)
  }

  return (
    <main style={{ minHeight: '100dvh', padding: 32, background: '#e9eee9', color: '#182922' }}>
      <h1>完成态开发预览</h1>
      <p>下方为视觉样例数据，不执行导出，也不改变主界面的任务状态。</p>
      <div style={{ display: 'flex', flexWrap: 'wrap', gap: 12 }}>
        <button data-testid="review-wuling-blend" type="button" onClick={() => show('map02', 'blend')}>武陵 · 场景</button>
        <button data-testid="review-valley-blend" type="button" onClick={() => show('map01', 'blend')}>四号谷地 · 场景</button>
        <button data-testid="review-wuling-data" type="button" onClick={() => show('map02', 'data_package')}>武陵 · 数据包</button>
        <button data-testid="review-valley-data" type="button" onClick={() => show('map01', 'data_package')}>四号谷地 · 数据包</button>
      </div>
      <ExtractionCompleteOverlay
        key={run}
        visible={visible}
        mapId={mapId}
        mapName={mapId === 'map02' ? '武陵' : '四号谷地'}
        sectorCount={4}
        exportMode={exportMode}
        blenderBuild={exportMode === 'blend' ? sampleBuild(mapId) : null}
        outputDirectory={null}
        onFinished={() => setVisible(false)}
        reviewNotice="DEV / PREVIEW"
      />
    </main>
  )
}

type CompletionPreviewProps = {
  mapId: MapId
  onFinished: () => void
}

export function CompletionPreview({ mapId, onFinished }: CompletionPreviewProps) {
  return <ExtractionCompleteOverlay
    visible
    mapId={mapId}
    mapName={mapId === 'map02' ? '武陵' : '四号谷地'}
    sectorCount={4}
    exportMode="blend"
    blenderBuild={sampleBuild(mapId)}
    outputDirectory={null}
    onFinished={onFinished}
    reviewNotice="DEV / PREVIEW"
  />
}
