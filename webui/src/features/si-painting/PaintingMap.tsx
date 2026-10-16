import { useEffect, useLayoutEffect, useRef, useState } from 'react'
import type { CSSProperties, PointerEvent, KeyboardEvent } from 'react'
import { useReducedMotion } from 'motion/react'
import { clamp, damp, phase, inkWedge, PAINTING_ART } from './artwork'
import type { LayerState } from '../../types'
import type { PaintingSelection } from './types'
import type { LevelMap } from './levelMaps'
import type { CanvasMapSnapshot } from '../../lib/mapSnapshot'

type Point = { x: number; y: number }
type Props = {
  active: boolean
  previousMap: CanvasMapSnapshot | null
  overviewUrl: string | null
  metadata: LevelMap | null
  preview: boolean
  onImageReady: (ready: boolean) => void
  onHandoffChange: (busy: boolean) => void
  onImageError: (message: string | null) => void
  layers: LayerState
  selection: PaintingSelection
  onSelectionChange: (selection: PaintingSelection) => void
  revision: number
  disabled: boolean
}

export function PaintingMap({ active, previousMap, overviewUrl, metadata, preview, onImageReady, onImageError, onHandoffChange, layers, selection, onSelectionChange, revision, disabled }: Props) {
  const svgRef = useRef<SVGSVGElement>(null)
  const stageRef = useRef<HTMLElement>(null)
  const oldMaskRef = useRef<SVGPathElement>(null)
  const [previousOffset, setPreviousOffset] = useState({ x: 0, y: 0 })
  const startPoint = useRef<Point | null>(null)
  const [zoom, setZoom] = useState(1)
  const [replay, setReplay] = useState(0)
  const [entering, setEntering] = useState(true)
  const [showPrevious, setShowPrevious] = useState(true)
  const reducedMotion = useReducedMotion() === true
  const [imageReady, setImageReady] = useState<string | null>(null)
  const [imageError, setImageError] = useState<string | null>(null)
  const sourceReady = overviewUrl ? imageReady === overviewUrl : preview
  useEffect(() => { onHandoffChange(active && sourceReady && entering) }, [active, sourceReady, entering, onHandoffChange])
  const ratio = metadata ? metadata.image.pixelWidth / metadata.image.pixelHeight : 1
  const frameWidth = Math.min(800, 800 * ratio)
  const frameHeight = Math.min(800, 800 / ratio)
  const frameX = (800 - frameWidth) / 2
  const frameY = (800 - frameHeight) / 2
  const cellWidth = metadata ? 1 / metadata.selectionGrid.columns : .0625
  const cellHeight = metadata ? 1 / metadata.selectionGrid.rows : .0625

  useEffect(() => {
    onImageReady(Boolean(overviewUrl && imageReady === overviewUrl && !imageError))
    onImageError(imageError)
  }, [overviewUrl, imageReady, imageError, onImageReady, onImageError])
  useEffect(() => { setImageError(null) }, [overviewUrl])

  useEffect(() => {
    const canvas = svgRef.current?.parentElement
    if (!canvas || !active) return
    const onWheel = (event: WheelEvent): void => {
      event.preventDefault()
      if (!disabled) setZoom(value => Math.min(2, Math.max(.65, value + (event.deltaY < 0 ? .08 : -.08))))
    }
    canvas.addEventListener('wheel', onWheel, { passive: false })
    return () => canvas.removeEventListener('wheel', onWheel)
  }, [active, disabled])

  useLayoutEffect(() => { setZoom(1) }, [revision])
  useLayoutEffect(() => { setShowPrevious(true); if (active) setEntering(true) }, [active, previousMap])
  useLayoutEffect(() => {
    if (!active || !previousMap || !stageRef.current) return
    const bounds = stageRef.current.getBoundingClientRect()
    setPreviousOffset({ x: previousMap.left - bounds.left, y: previousMap.top - bounds.top })
  }, [active, previousMap])
  useLayoutEffect(() => {
    const svg = svgRef.current
    if (!svg) return
    const requireNode = (id: string): SVGElement => {
      const node = svg.querySelector(`#si-${id}`)
      if (!(node instanceof SVGElement)) throw new Error(`Painting entrance element missing: ${id}`)
      return node
    }
    const reveal = requireNode('reveal-path')
    const band = requireNode('ink-band')
    const front = requireNode('ink-front')
    const fray = requireNode('ink-fray')
    const content = requireNode('map-new')
    const oldMask = oldMaskRef.current
    reveal.setAttribute('d', 'M0 800H800V800H0Z')
    for (const node of [band, front, fray]) node.style.opacity = '0'
    content.style.opacity = '0'
    oldMask?.setAttribute('d', 'M-10000 -10000H10000V10000H-10000Z')
    if (!sourceReady || !active) return
    if (oldMask && stageRef.current) {
      const bounds = stageRef.current.getBoundingClientRect()
      const frame = svg.getBoundingClientRect()
      oldMask.setAttribute('transform', `translate(${frame.left - bounds.left} ${frame.top - bounds.top}) scale(${frame.width / 800} ${frame.height / 800})`)
    }
    let frame = 0
    const started = performance.now()
    setEntering(true)
    const tick = (now: number): void => {
      const t = reducedMotion ? 1 : clamp((now - started) / 820)
      const p = t < .5 ? 4 * t ** 3 : 1 - (-2 * t + 2) ** 3 / 2
      const wedge = inkWedge(p)
      oldMask?.setAttribute('d', `M-10000 -10000H10000V10000H-10000Z ${wedge}`)
      reveal.setAttribute('d', t === 1 ? 'M0 0H800V800H0Z' : wedge)
      band.setAttribute('d', wedge)
      band.style.opacity = String(t < 1 ? (1 - damp(phase(t, .38, .54))) * .65 : 0)
      front.setAttribute('d', wedge)
      front.style.opacity = String(t < .78 ? .17 : 0)
      const tipX = 340 + 500 * p
      const tipY = 820 - 2000 * p ** 1.25
      fray.setAttribute('d', Array.from({ length: 12 }, (_, k) => {
        const offset = (k - 5.5) * 8 * p
        const endX = tipX - (150 + k * 17) * p + offset * 4
        const endY = tipY + (290 + k * 23) * p
        return `M${tipX + offset} ${tipY + 55 + k * 8}Q${tipX - 35 * p + offset * 2} ${tipY + 160 * p} ${endX} ${endY}L${endX + 2.5} ${endY - 35 * p}Z`
      }).join(''))
      fray.style.opacity = String(t < .78 ? .16 * Math.sin(t * Math.PI) : 0)
      content.style.opacity = String(.35 + p * .65)
      content.setAttribute('transform', `translate(${24 * (1 - p)} ${44 * (1 - p)}) scale(${.94 + p * .06})`)
      if (t < 1) frame = requestAnimationFrame(tick)
      else setEntering(false)
    }
    tick(started)
    return () => cancelAnimationFrame(frame)
  }, [reducedMotion, replay, revision, sourceReady, overviewUrl, active, previousMap])

  const point = (event: PointerEvent<HTMLDivElement>): Point => {
    const svg = svgRef.current
    const matrix = svg?.getScreenCTM()
    if (!svg || !matrix) throw new Error('Painting map has no valid screen transform')
    const mapped = new DOMPoint(event.clientX, event.clientY).matrixTransform(matrix.inverse())
    return { x: clamp(((mapped.x - 400) / zoom + 400 - frameX) / frameWidth), y: clamp(((mapped.y - 400) / zoom + 400 - frameY) / frameHeight) }
  }
  const onPointerDown = (event: PointerEvent<HTMLDivElement>): void => {
    if (disabled || entering || (!metadata && !preview) || event.button !== 0) return
    const start = point(event)
    startPoint.current = start
    onSelectionChange({ u: Math.min(1 - cellWidth, Math.floor(start.x / cellWidth) * cellWidth), v: Math.min(1 - cellHeight, Math.floor(start.y / cellHeight) * cellHeight), width: cellWidth, height: cellHeight })
    event.currentTarget.setPointerCapture(event.pointerId)
  }
  const onPointerMove = (event: PointerEvent<HTMLDivElement>): void => {
    const start = startPoint.current
    if (!start || disabled) return
    const end = point(event)
    if (Math.abs(end.x - start.x) < .00001 || Math.abs(end.y - start.y) < .00001) return
    onSelectionChange({ u: Math.min(start.x, end.x), v: Math.min(start.y, end.y), width: Math.abs(end.x - start.x), height: Math.abs(end.y - start.y) })
  }
  const finishSelection = (): void => { startPoint.current = null }
  const onKeyDown = (event: KeyboardEvent<HTMLDivElement>): void => {
    if (disabled || entering || (!metadata && !preview)) return
    const delta: Record<string, Point> = { ArrowLeft: { x: -cellWidth, y: 0 }, ArrowRight: { x: cellWidth, y: 0 }, ArrowUp: { x: 0, y: -cellHeight }, ArrowDown: { x: 0, y: cellHeight } }
    const change = delta[event.key]
    if (!change) return
    event.preventDefault()
    onSelectionChange({ ...selection, u: Math.min(1 - selection.width, Math.max(0, selection.u + change.x)), v: Math.min(1 - selection.height, Math.max(0, selection.v + change.y)) })
  }
  const setBoundedZoom = (value: number): void => setZoom(Math.min(2, Math.max(.65, value)))
  return <section ref={stageRef} className="si-map-stage" aria-label="祀的画地图">
    <div className="si-stage-grid" /><div className="si-stage-tag"><i />PAINTED REALM / {overviewUrl ? 'REGION OVERVIEW' : '素材待接入'}</div><div className="si-stage-index">03 / ATLAS</div>
    <div className="si-map-square" data-testid="si-map-canvas" tabIndex={0} aria-label="地图框选，方向键移动选区" onPointerDown={onPointerDown} onPointerMove={onPointerMove} onPointerUp={finishSelection} onPointerCancel={finishSelection} onLostPointerCapture={finishSelection} onKeyDown={onKeyDown}>
      <svg ref={svgRef} viewBox="0 0 800 800" aria-hidden="true" data-entering={entering}>
        <defs><clipPath id="si-reveal"><path id="si-reveal-path" d="M0 800H800V800H0Z" /></clipPath><pattern id="si-map-grid" width="50" height="50" patternUnits="userSpaceOnUse"><path d="M50 0H0V50" fill="none" stroke="#b8c7b8" strokeOpacity=".11" strokeWidth=".6" /></pattern></defs>
        <g clipPath="url(#si-reveal)"><rect width="800" height="800" fill="#191c18" /><g id="si-map-new"><g transform={`translate(${400 * (1 - zoom)} ${400 * (1 - zoom)}) scale(${zoom})`}>
          <g transform={`translate(${frameX} ${frameY}) scale(${frameWidth / 800} ${frameHeight / 800})`}>
            {overviewUrl ? <image data-testid="si-real-map-image" href={overviewUrl} width="800" height="800" preserveAspectRatio="none" onLoad={() => { setImageReady(overviewUrl); setImageError(null) }} onError={() => { setImageReady(null); setImageError(`地图图像加载失败：${overviewUrl}`) }} /> : preview ? <g className="si-preview-layers" style={{ '--terrain': layers.terrain ? 1 : 0, '--structures': layers.instances ? 1 : 0, '--landscape': layers.water ? 1 : 0, '--effects': layers.effects ? 1 : 0 } as CSSProperties} dangerouslySetInnerHTML={{ __html: PAINTING_ART }} /> : null}
            {metadata && <g fill="none" stroke="#f4f1ea" strokeOpacity=".16" strokeWidth=".6" vectorEffect="non-scaling-stroke">{Array.from({ length: metadata.selectionGrid.columns + 1 }, (_, i) => <path key={`x${i}`} d={`M${i * 800 * cellWidth} 0V800`} />)}{Array.from({ length: metadata.selectionGrid.rows + 1 }, (_, i) => <path key={`y${i}`} d={`M0 ${i * 800 * cellHeight}H800`} />)}</g>}
            {(metadata || preview) && <rect data-testid="si-selection-rect" x={selection.u * 800} y={selection.v * 800} width={selection.width * 800} height={selection.height * 800} fill="#d95338" fillOpacity=".28" stroke="#f4ead8" strokeWidth="2" vectorEffect="non-scaling-stroke" />}
          </g>
        </g></g></g>
        <path id="si-ink-band" fill="#081b15" stroke="#101813" strokeWidth="7" opacity="0" />
        <path id="si-ink-fray" clipPath="url(#si-reveal)" fill="#b4bba0" opacity="0" />
        <path id="si-ink-front" fill="none" stroke="#48c4a8" strokeWidth="1.3" opacity="0" />
      </svg>
    </div>
    {active && showPrevious && previousMap && !imageError && <svg className="si-map-handoff" data-testid="si-map-previous" style={{ visibility: entering ? 'visible' : 'hidden' }} aria-hidden="true">
      <defs><clipPath id="si-old-map-mask" clipPathUnits="userSpaceOnUse"><path ref={oldMaskRef} d="M-10000 -10000H10000V10000H-10000Z" clipRule="evenodd" /></clipPath></defs>
      <image href={previousMap.image} x={previousOffset.x} y={previousOffset.y} width={previousMap.width} height={previousMap.height} clipPath="url(#si-old-map-mask)" />
    </svg>}
    {imageError && <p className="si-map-error" role="alert">{imageError}</p>}
    <button type="button" className="si-replay-entrance" data-testid="si-replay-entrance" onClick={() => { setShowPrevious(false); setReplay(value => value + 1) }}>↗ 重播入画</button>
    <div className="si-map-tools"><button type="button" data-testid="si-zoom-in" aria-label="放大地图" onClick={() => setBoundedZoom(zoom + .15)}>+</button><button type="button" data-testid="si-zoom-out" aria-label="缩小地图" onClick={() => setBoundedZoom(zoom - .15)}>−</button><button type="button" data-testid="si-zoom-reset" aria-label="复位地图视图" onClick={() => setZoom(1)}>⌖</button></div>
    <div className="si-map-instructions">DRAG / 框选 <span>WHEEL / 缩放</span><b>{Math.round(zoom * 100)}%</b></div>
    {!overviewUrl && <div className="si-map-scale"><i>{preview ? '构图占位 · 不能用于真实导出' : '画境地图尚未准备'}</i></div>}
  </section>
}
