import { useEffect, useRef } from 'react'
import './AmbientContours.css'

const CONTOUR_COUNT = 28
const CONTOUR_STEP = 0.17
const FIRST_CONTOUR = -1.25
const MAX_GRID_NODES = 10_000
const MAX_BACKING_PIXELS = 9_000_000
const FRAME_INTERVAL_MS = 50
const FIELD_CYCLE_MS = 20_000

const elevation = (x: number, y: number, phase: number) => {
  const u = x
    + 0.038 * Math.sin(y * 19)
    + 0.016 * Math.cos(x * 31 + y * 7)
    + 0.009 * Math.sin(y * 13 + phase)
  const v = y
    + 0.028 * Math.sin(x * 23 - y * 5)
    + 0.007 * Math.sin(x * 17 + y * 9 - phase)
  const peakShift = Math.sin(phase)
  const crossShift = Math.cos(phase)

  return 2.9 * Math.exp(-((u - (0.08 + 0.007 * peakShift)) ** 2 * 72 + (v - (0.18 + 0.006 * crossShift)) ** 2 * 38))
    + 2.1 * Math.exp(-((u - (0.28 + 0.006 * crossShift)) ** 2 * 95 + (v - (0.02 + 0.004 * peakShift)) ** 2 * 60))
    + 3.2 * Math.exp(-((u - (0.87 + 0.006 * crossShift)) ** 2 * 55 + (v - (0.91 + 0.005 * peakShift)) ** 2 * 65))
    + 1.8 * Math.exp(-((u - (0.98 + 0.005 * peakShift)) ** 2 * 120 + (v - (0.31 + 0.006 * crossShift)) ** 2 * 45))
    - 1.5 * Math.exp(-((u - (0.2 + 0.006 * crossShift)) ** 2 * 65 + (v - (0.86 + 0.008 * peakShift)) ** 2 * 52))
    + 0.24 * Math.sin(u * 14 + v * 8) + 0.1 * Math.cos(v * 21 - u * 11)
}

function resolveGridStep(width: number, height: number) {
  let step = Math.max(12, Math.ceil(Math.sqrt(width * height / 8_500)))
  let columns = Math.ceil(width / step) + 1
  let rows = Math.ceil(height / step) + 1

  while (columns * rows > MAX_GRID_NODES) {
    step += 1
    columns = Math.ceil(width / step) + 1
    rows = Math.ceil(height / step) + 1
  }

  return step
}

function traceTriangle(
  context: CanvasRenderingContext2D,
  level: number,
  x1: number,
  y1: number,
  value1: number,
  x2: number,
  y2: number,
  value2: number,
  x3: number,
  y3: number,
  value3: number,
) {
  let hitCount = 0
  let firstX = 0
  let firstY = 0

  if ((value1 < level) !== (value2 < level)) {
    const ratio = (level - value1) / (value2 - value1)
    firstX = x1 + ratio * (x2 - x1)
    firstY = y1 + ratio * (y2 - y1)
    hitCount = 1
  }

  if ((value2 < level) !== (value3 < level)) {
    const ratio = (level - value2) / (value3 - value2)
    const hitX = x2 + ratio * (x3 - x2)
    const hitY = y2 + ratio * (y3 - y2)
    if (hitCount === 0) {
      firstX = hitX
      firstY = hitY
      hitCount = 1
    } else {
      context.moveTo(firstX, firstY)
      context.lineTo(hitX, hitY)
      hitCount = 2
    }
  }

  if ((value3 < level) !== (value1 < level)) {
    const ratio = (level - value3) / (value1 - value3)
    const hitX = x3 + ratio * (x1 - x3)
    const hitY = y3 + ratio * (y1 - y3)
    if (hitCount === 0) {
      firstX = hitX
      firstY = hitY
    } else {
      context.moveTo(firstX, firstY)
      context.lineTo(hitX, hitY)
    }
  }
}

export function AmbientContours() {
  const canvasRef = useRef<HTMLCanvasElement | null>(null)

  useEffect(() => {
    const canvas = canvasRef.current
    if (!canvas) throw new Error('The ambient contour canvas element is missing.')

    const context = canvas.getContext('2d')
    if (!context) throw new Error('The ambient contour canvas could not initialize its 2D context.')

    let width = 0
    let height = 0
    let step = 12
    let columns = 0
    let rows = 0
    let pixelRatio = 1
    let samples = new Float32Array(0)
    let phase = 0
    let lastFrameAt = 0
    let animationFrame = 0
    let resizeFrame = 0
    let resolutionQuery = window.matchMedia(`(resolution: ${window.devicePixelRatio || 1}dppx)`)
    const reducedMotionQuery = window.matchMedia('(prefers-reduced-motion: reduce)')

    const drawContours = (nextPhase: number) => {
      if (width < 1 || height < 1 || columns < 2 || rows < 2) return

      for (let row = 0; row < rows; row += 1) {
        const y = Math.min(row * step, height)
        const normalizedY = y / height
        const rowOffset = row * columns
        for (let column = 0; column < columns; column += 1) {
          const x = Math.min(column * step, width)
          samples[rowOffset + column] = elevation(x / width, normalizedY, nextPhase)
        }
      }

      context.clearRect(0, 0, width, height)
      context.strokeStyle = '#69776c'
      context.globalAlpha = 0.32

      for (let contour = 0; contour < CONTOUR_COUNT; contour += 1) {
        const level = FIRST_CONTOUR + contour * CONTOUR_STEP
        context.lineWidth = contour % 6 === 0 ? 0.9 : 0.6
        context.beginPath()

        for (let row = 0; row < rows - 1; row += 1) {
          const y1 = Math.min(row * step, height)
          const y2 = Math.min((row + 1) * step, height)
          const rowOffset = row * columns
          const nextRowOffset = rowOffset + columns

          for (let column = 0; column < columns - 1; column += 1) {
            const topLeft = samples[rowOffset + column]
            const topRight = samples[rowOffset + column + 1]
            const bottomRight = samples[nextRowOffset + column + 1]
            const bottomLeft = samples[nextRowOffset + column]
            const cellMinimum = Math.min(topLeft, topRight, bottomRight, bottomLeft)
            const cellMaximum = Math.max(topLeft, topRight, bottomRight, bottomLeft)
            if (level <= cellMinimum || level > cellMaximum) continue

            const x1 = Math.min(column * step, width)
            const x2 = Math.min((column + 1) * step, width)
            const firstTriangleMinimum = Math.min(topLeft, topRight, bottomRight)
            const firstTriangleMaximum = Math.max(topLeft, topRight, bottomRight)
            if (level > firstTriangleMinimum && level <= firstTriangleMaximum) {
              traceTriangle(context, level, x1, y1, topLeft, x2, y1, topRight, x2, y2, bottomRight)
            }

            const secondTriangleMinimum = Math.min(topLeft, bottomRight, bottomLeft)
            const secondTriangleMaximum = Math.max(topLeft, bottomRight, bottomLeft)
            if (level > secondTriangleMinimum && level <= secondTriangleMaximum) {
              traceTriangle(context, level, x1, y1, topLeft, x2, y2, bottomRight, x1, y2, bottomLeft)
            }
          }
        }

        context.stroke()
      }
    }

    const resize = () => {
      width = window.innerWidth
      height = window.innerHeight
      if (width < 1 || height < 1) return

      const areaPixelRatio = Math.sqrt(MAX_BACKING_PIXELS / (width * height))
      pixelRatio = Math.min(window.devicePixelRatio || 1, 2, areaPixelRatio)
      canvas.width = Math.round(width * pixelRatio)
      canvas.height = Math.round(height * pixelRatio)
      context.setTransform(pixelRatio, 0, 0, pixelRatio, 0, 0)

      step = resolveGridStep(width, height)
      columns = Math.ceil(width / step) + 1
      rows = Math.ceil(height / step) + 1
      samples = new Float32Array(columns * rows)
      drawContours(reducedMotionQuery.matches ? 0 : phase)
    }

    const scheduleResize = () => {
      window.cancelAnimationFrame(resizeFrame)
      resizeFrame = window.requestAnimationFrame(() => {
        resizeFrame = 0
        resize()
      })
    }

    const stopAnimation = () => {
      window.cancelAnimationFrame(animationFrame)
      animationFrame = 0
      lastFrameAt = 0
    }

    const renderFrame = (timestamp: number) => {
      animationFrame = 0
      if (document.hidden || reducedMotionQuery.matches) return

      if (lastFrameAt === 0) {
        lastFrameAt = timestamp
      } else if (timestamp - lastFrameAt >= FRAME_INTERVAL_MS) {
        const elapsed = Math.min(timestamp - lastFrameAt, 100)
        phase = (phase + Math.PI * 2 * elapsed / FIELD_CYCLE_MS) % (Math.PI * 2)
        lastFrameAt = timestamp
        drawContours(phase)
      }

      animationFrame = window.requestAnimationFrame(renderFrame)
    }

    const startAnimation = () => {
      if (document.hidden || reducedMotionQuery.matches || animationFrame !== 0) return
      lastFrameAt = 0
      animationFrame = window.requestAnimationFrame(renderFrame)
    }

    const handleVisibilityChange = () => {
      if (document.hidden) stopAnimation()
      else startAnimation()
    }

    const handleMotionPreferenceChange = () => {
      stopAnimation()
      if (reducedMotionQuery.matches) {
        phase = 0
        drawContours(phase)
      } else {
        startAnimation()
      }
    }

    const handleResolutionChange = () => {
      resolutionQuery.removeEventListener('change', handleResolutionChange)
      resolutionQuery = window.matchMedia(`(resolution: ${window.devicePixelRatio || 1}dppx)`)
      resolutionQuery.addEventListener('change', handleResolutionChange)
      scheduleResize()
    }

    resize()
    if (!reducedMotionQuery.matches) startAnimation()
    window.addEventListener('resize', scheduleResize)
    document.addEventListener('visibilitychange', handleVisibilityChange)
    reducedMotionQuery.addEventListener('change', handleMotionPreferenceChange)
    resolutionQuery.addEventListener('change', handleResolutionChange)

    return () => {
      stopAnimation()
      window.cancelAnimationFrame(resizeFrame)
      window.removeEventListener('resize', scheduleResize)
      document.removeEventListener('visibilitychange', handleVisibilityChange)
      reducedMotionQuery.removeEventListener('change', handleMotionPreferenceChange)
      resolutionQuery.removeEventListener('change', handleResolutionChange)
    }
  }, [])

  return <canvas ref={canvasRef} className="ambient-contours-canvas" aria-hidden="true" />
}
