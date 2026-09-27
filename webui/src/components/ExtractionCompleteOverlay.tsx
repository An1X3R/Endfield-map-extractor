import { useEffect, useRef, useState } from 'react'
import { useReducedMotion } from 'motion/react'
import type { BlenderBuildResult, ExportMode, MapId } from '../types'
import assemblySvg from './completion/assembly.svg?raw'
import filtersSvg from './completion/filters.svg?raw'
import infrastructureSvg from './completion/infrastructure.svg?raw'
import landmarkSvg from './completion/landmark.svg?raw'
import landscapeSvg from './completion/recovery-landscape.svg?raw'
import matrixSvg from './completion/recovery-matrix.svg?raw'
import reliefUrl from './completion/wuling-relief.svg'
import { playCompletion, REPORT, sampleAscii } from './completion/animation'
import './completion/complete.css'

type Props = {
  visible: boolean
  mapId: MapId
  mapName: string
  sectorCount: number
  exportMode: ExportMode
  blenderBuild: BlenderBuildResult | null
  outputDirectory: string | null
  onFinished: () => void
  reviewNotice?: string
}

export function ExtractionCompleteOverlay({
  visible,
  mapId,
  mapName,
  sectorCount,
  exportMode,
  blenderBuild,
  outputDirectory,
  onFinished,
  reviewNotice,
}: Props) {
  const reducedMotion = useReducedMotion() === true
  const [asciiRows, setAsciiRows] = useState<string[] | null>(null)
  const [artError, setArtError] = useState<string | null>(null)
  const [replay, setReplay] = useState(0)
  const [dither, setDither] = useState(true)
  const ditherRef = useRef(dither)
  ditherRef.current = dither
  const cardRef = useRef<HTMLDivElement>(null)
  const dialogRef = useRef<HTMLDialogElement>(null)
  const closeRef = useRef<HTMLButtonElement>(null)

  const scenes = blenderBuild?.scenes ?? []
  const pending = blenderBuild?.pending ?? (scenes.length > 0 ? scenes.reduce((sum, scene) => sum + scene.pending, 0) : null)
  const partial = blenderBuild?.status === 'partial' || (pending !== null && pending > 0)
  const missingScenes = exportMode === 'blend' && scenes.length === 0
  const title = exportMode === 'data_package'
    ? '审计数据包已导出'
    : missingScenes ? '任务结束 · 场景报告缺失' : '场景已导出'
  const subtitle = exportMode === 'data_package'
    ? '所选分组、区块索引与审计记录已写入数据包'
    : missingScenes ? '后端未返回场景路径；请检查导出报告'
      : partial ? pending === null ? '场景有待处理项，详见下方场景报告' : `${pending} 项待处理，详见下方场景报告`
        : pending === null ? `${scenes.length} 批场景文件已返回；待处理数未返回`
          : `${scenes.length} 批场景文件已返回`

  useEffect(() => {
    if (!visible || mapId !== 'map02') return
    let active = true
    const image = new Image()
    image.src = reliefUrl
    void image.decode().then(() => {
      if (!active) return
      setAsciiRows(sampleAscii(image))
      setArtError(null)
    }).catch((error: Error) => {
      if (!active) return
      setArtError(`武陵地形装饰加载失败：${error.message}`)
    })
    return () => { active = false }
  }, [mapId, visible])

  useEffect(() => {
    if (!visible) return
    const dialog = dialogRef.current
    if (!dialog) throw new Error('Completion dialog is missing')
    dialog.showModal()
    closeRef.current?.focus()
    return () => dialog.close()
  }, [visible])

  useEffect(() => {
    if (!visible || (mapId === 'map02' && !asciiRows)) return
    const card = cardRef.current
    if (!card) throw new Error('Completion card is missing')
    return playCompletion(card, mapId, asciiRows, reducedMotion, ditherRef.current)
  }, [visible, mapId, asciiRows, replay, reducedMotion])

  if (!visible) return null

  return (
    <dialog className="ec-overlay" ref={dialogRef} aria-label={`${mapName}导出结果`} onCancel={(event) => { event.preventDefault(); onFinished() }}>
      <div className={`ec-card ec-card--${mapId}`} data-dither={dither ? 'on' : 'off'} ref={cardRef}>
        <div className="ec-filters" dangerouslySetInnerHTML={{ __html: filtersSvg }} aria-hidden="true" />
        {mapId === 'map02' ? (
          <div className="ec-art ec-art--wuling" aria-hidden="true">
            <div className="ec-recovery-landscape" dangerouslySetInnerHTML={{ __html: landscapeSvg }} />
            <div className="ec-recovery-light" />
            <div className="ec-recovery-arrows"><i>↑</i><i>↑</i><i>↑</i></div>
            <div className="ec-recovery-matrix" dangerouslySetInnerHTML={{ __html: matrixSvg }} />
            <span className="ec-art-caption">WULING / SECTOR TRANSFER</span>
            {artError && <span className="ec-art-error">{artError}</span>}
          </div>
        ) : (
          <div className="ec-art ec-art--valley" aria-hidden="true">
            <div className="ec-valley-assembly" dangerouslySetInnerHTML={{ __html: assemblySvg }} />
            <div className="ec-valley-infrastructure" dangerouslySetInnerHTML={{ __html: infrastructureSvg }} />
            <div className="ec-valley-landmark" dangerouslySetInnerHTML={{ __html: landmarkSvg }} />
            <div className="ec-valley-title"><small>01 / 四号谷地</small><strong>VALLEY <span>IV</span></strong></div>
            <span className="ec-art-caption">区域构建结束 / INDUSTRIAL ASSEMBLY</span>
            <i className="ec-valley-scan ec-valley-scan--left" />
            <i className="ec-valley-scan ec-valley-scan--right" />
          </div>
        )}
        <div className="ec-content">
          {mapId === 'map02' && <svg className="ec-ascii" viewBox="0 0 660 608" xmlSpace="preserve" aria-hidden="true" />}
          <aside className="ec-report" aria-hidden="true"><small>工区档案 / FIELD RECORD</small><pre className="ec-report-text">{REPORT}</pre></aside>
          <button className="ec-close" data-testid="completion-close" type="button" ref={closeRef} onClick={onFinished} aria-label="关闭导出结果">×</button>
          <div className="ec-overline"><span>{reviewNotice ?? 'EXPORT / RESULT'}</span><span>{exportMode === 'blend' ? 'BLENDER SCENE' : 'DATA PACKAGE'}</span></div>
          <div className="ec-seal"><svg viewBox="0 0 60 60" aria-hidden="true"><path className="ec-seal-frame" d="M8 24V8h16M36 8h16v16M52 36v16H36M24 52H8V36" /><path className="ec-check" d="m17 29 9 9 18-20" pathLength="1" /></svg><span>导出流程结束<small>{mapName} / {sectorCount} SECTORS</small></span></div>
          <div className="ec-title-wrap"><h2 className="ec-title">{title}</h2></div>
          <p className="ec-subtitle">{subtitle}</p>
          <div className="ec-rule" />
          <div className="ec-details">
            {exportMode === 'data_package' ? <p><b>已完成</b><span>数据包导出；实际内容以导出报告为准</span></p> : (
              <>
                <p><b>场景结果</b><span>{scenes.length > 0 ? `${scenes.length} 批场景文件` : '未返回场景路径'}</span></p>
                <p><b>待处理</b><span>{pending === null ? '数量未返回' : `${pending} 项`}；待处理不等同于缺失资产数量</span></p>
              </>
            )}
          </div>
          {(outputDirectory || exportMode === 'blend' && scenes.length > 0) && (
            <div className="ec-scene-list" aria-label="导出文件路径">
              {outputDirectory && <p><code data-testid="completion-output-directory" title={outputDirectory}>{outputDirectory}</code><small>输出目录</small></p>}
              {exportMode === 'blend' && scenes.map((scene) => <p key={scene.path}><code title={scene.path}>{scene.path}</code><small>{scene.instances} 实例 · {scene.pending} 待处理</small></p>)}
            </div>
          )}
          <div className="ec-actions">
            <div className="ec-tools"><button data-testid="completion-replay" type="button" onClick={() => setReplay((value) => value + 1)}>重播动画 ↻</button><button data-testid="completion-filter" type="button" aria-pressed={dither} onClick={() => setDither((value) => !value)}>像素滤镜 · {dither ? '开' : '关'}</button></div>
            <button className="ec-return" data-testid="completion-return" type="button" onClick={onFinished}>返回地图 →</button>
          </div>
        </div>
      </div>
    </dialog>
  )
}
