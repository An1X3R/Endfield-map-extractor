import { OPERATOR_COMPLETION_DURATION } from './completionMotion'

export interface ScanFrameDetail {
  readonly progress: number
  readonly printProgress: number
  readonly scanY: number
  readonly partial: boolean
}

export interface ScanFrameEvent extends CustomEvent<ScanFrameDetail> {
  readonly type: 'scanframe'
}

export interface ScanController {
  play(milliseconds: number, reduced: boolean, isPartial: boolean): void
  reset(): void
  setProgress(value: number, isPartial: boolean): void
  dispose(): void
}

declare global {
  interface HTMLElementEventMap {
    scanframe: ScanFrameEvent
  }
}

const clamp = (value: number, min: number, max: number): number => Math.max(min, Math.min(max, value))
/** Render the plate and drive the shared window exposure clock. */
export function createScan(canvas: HTMLCanvasElement, artworkUrl: string | null): ScanController {
  if (!(canvas instanceof HTMLCanvasElement)) throw new TypeError('Portrait reveal requires a canvas')
  const context = canvas.getContext('2d')
  if (!context) throw new Error('Portrait canvas context is unavailable')

  const artwork = new Image()
  let artworkReady = artworkUrl === null
  let width = 0
  let height = 0
  let progress = 0
  let elapsed = 0
  let duration = 0
  let start = 0
  let animationFrame = 0
  let partial = false
  let disposed = false
  let artworkFailure: Error | null = null

  const render = (): void => {
    if (!width || !height || disposed) return
    context.clearRect(0, 0, width, height)
    if (!artworkReady) return

    const reveal = progress
    const sourceWidth = artworkUrl ? artwork.naturalWidth : 640
    const sourceHeight = artworkUrl ? artwork.naturalHeight : 720
    const scale = Math.max(0, Math.min((width - 32) / sourceWidth, (height - 40) / sourceHeight))
    const scaledWidth = sourceWidth * scale
    const scaledHeight = sourceHeight * scale
    const x = (width - scaledWidth) * 0.5
    const y = (height - scaledHeight) * 0.47
    canvas.dataset.progress = progress.toFixed(4)
    canvas.dataset.printProgress = reveal.toFixed(4)
    // The window-wide exposure mask owns visibility, including the portrait.
    if (artworkUrl) context.drawImage(artwork, x, y, scaledWidth, scaledHeight)
    else {
      context.strokeStyle = '#83919f'
      context.lineWidth = 1
      context.strokeRect(width * .3, height * .3, width * .4, height * .4)
      context.font = '11px Consolas,monospace'
      context.textAlign = 'center'
      context.fillStyle = '#a1acbc'
      context.fillText('PORTRAIT / UNASSIGNED', width * .5, height * .51)
      context.textAlign = 'start'
    }
    context.fillStyle = '#a1acbc'
    context.font = '9px Consolas,monospace'
    context.fillText('PLATE 01 / PORTRAIT REGISTER', 24, 28)
    context.fillText('ENTITY ARCHIVE / REGISTERED', 24, height - 24)
    canvas.dispatchEvent(new CustomEvent<ScanFrameDetail>('scanframe', {
      detail: { progress, printProgress: reveal, scanY: y + scaledHeight * reveal, partial },
    }))
  }

  const stop = (): void => {
    cancelAnimationFrame(animationFrame)
    animationFrame = 0
  }

  const resize = (): void => {
    const bounds = canvas.getBoundingClientRect()
    width = bounds.width
    height = bounds.height
    const ratio = Math.min(devicePixelRatio, 2)
    canvas.width = Math.round(width * ratio)
    canvas.height = Math.round(height * ratio)
    context.setTransform(ratio, 0, 0, ratio, 0, 0)
    render()
  }

  const animate = (time: number): void => {
    if (disposed) return
    if (!artworkReady) { animationFrame = requestAnimationFrame(animate); return }
    if (!start) start = time
    elapsed = time - start
    progress = clamp(elapsed / duration, 0, 1)
    render()
    if (progress < 1) animationFrame = requestAnimationFrame(animate)
  }

  artwork.onload = (): void => {
    artworkReady = true
    render()
  }
  artwork.onerror = (): void => {
    stop()
    artworkFailure = new Error(`Failed to load operator portrait: ${artworkUrl}`)
    throw artworkFailure
  }

  const observer = new ResizeObserver(resize)
  observer.observe(canvas)
  if (artworkUrl) artwork.src = artworkUrl

  return {
    play(milliseconds: number, reduced: boolean, isPartial: boolean): void {
      if (disposed) throw new Error('Portrait reveal disposed')
      if (artworkFailure) throw artworkFailure
      if (!Number.isFinite(milliseconds) || milliseconds < 0) throw new RangeError('Invalid reveal duration')
      stop()
      partial = isPartial
      start = 0
      elapsed = 0
      duration = milliseconds
      progress = reduced || milliseconds === 0 ? 1 : 0
      resize()
      if (progress < 1) animationFrame = requestAnimationFrame(animate)
    },
    reset(): void {
      stop()
      progress = 0
      elapsed = 0
      render()
    },
    setProgress(value: number, isPartial: boolean): void {
      if (!Number.isFinite(value) || value < 0 || value > 1) throw new RangeError('Progress must be within 0..1')
      stop()
      progress = value
      elapsed = value * OPERATOR_COMPLETION_DURATION
      partial = isPartial
      resize()
    },
    dispose(): void {
      stop()
      observer.disconnect()
      artwork.onload = null
      artwork.onerror = null
      disposed = true
    },
  }
}
