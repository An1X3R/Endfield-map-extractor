export const OPERATOR_COMPLETION_DURATION = 1250

type CompletionMotion = {
  seek: (progress: number) => void
  exposure: () => number
  dispose: () => void
}

const clamp = (value: number): number => Math.max(0, Math.min(1, value))
const noise = (value: number): number => {
  const result = Math.sin(value * 127.1 + 311.7) * 43758.5453
  return result - Math.floor(result)
}

/** A monotonic damped sweep: no overshoot and no second settling timeline. */
function exposureAt(progress: number): number {
  const t = clamp(progress)
  return 1 - Math.pow(1 - t, 3)
}

/** Reveal every surface at the same physical cut, across both columns. */
export function createCompletionMotion(root: HTMLElement): CompletionMotion {
  const canvas = root.querySelector<HTMLCanvasElement>('.operator-window-rays')
  if (!canvas) throw new Error('Operator window ray canvas is missing')
  const context = canvas.getContext('2d')
  if (!context) throw new Error('Operator window ray context is unavailable')
  const surfaces = Array.from(root.querySelectorAll<HTMLElement>('.completion-layout, .completion-top > span'))
  if (surfaces.length !== 3) throw new Error('Operator exposure surfaces are incomplete')
  let exposure = 0
  let width = 0
  let height = 0
  root.dataset.windowExposure = 'active'

  return {
    exposure: (): number => exposure,
    seek(progress: number): void {
      exposure = exposureAt(progress)
      const bounds = root.getBoundingClientRect()
      const nextWidth = root.clientWidth
      const nextHeight = root.scrollHeight
      if (nextWidth !== width || nextHeight !== height) {
        width = nextWidth
        height = nextHeight
        canvas.style.height = `${height}px`
        const ratio = Math.min(devicePixelRatio, 2)
        canvas.width = Math.round(width * ratio)
        canvas.height = Math.round(height * ratio)
        context.setTransform(ratio, 0, 0, ratio, 0, 0)
      }
      const cut = height * exposure
      surfaces.forEach(surface => {
        const rect = surface.getBoundingClientRect()
        const localCut = cut - (rect.top - bounds.top - root.clientTop + root.scrollTop)
        const hidden = Math.max(0, Math.min(rect.height, rect.height - localCut))
        surface.style.clipPath = progress >= 1 ? 'none' : `inset(0 0 ${hidden}px 0)`
      })
      root.dataset.choreographyProgress = progress.toFixed(4)
      root.style.setProperty('--operator-scan-y', `${cut}px`)
      context.clearRect(0, 0, width, height)
      if (progress <= 0 || progress >= 1) return
      const strength = Math.min(1, progress * 18, (1 - progress) * 14)
      const tick = Math.floor(progress * OPERATOR_COMPLETION_DURATION / 32)
      const wash = context.createLinearGradient(0, cut - 22, 0, cut)
      wash.addColorStop(0, 'rgba(212,204,93,0)')
      wash.addColorStop(1, `rgba(212,204,93,${strength * .16})`)
      context.fillStyle = wash
      context.fillRect(0, cut - 22, width, 22)
      context.fillStyle = `rgba(201,186,45,${strength * .85})`
      context.fillRect(0, cut, width, 1)
      // Distributed edge origins converge on the shared print boundary.
      for (let index = 0; index < 18; index += 1) {
        const seed = tick * 43 + index * 71
        const edge = index % 4
        const location = noise(seed)
        const ax = edge === 0 ? 1 : edge === 1 ? width - 1 : location * width
        const ay = edge === 2 ? 1 : edge === 3 ? height - 1 : location * height
        const bx = noise(seed + 23) * width
        const beam = context.createLinearGradient(ax, ay, bx, cut)
        beam.addColorStop(0, `rgba(186,180,94,${strength * .06})`)
        beam.addColorStop(1, `rgba(190,175,61,${strength * .38})`)
        context.strokeStyle = beam
        context.lineWidth = .65
        context.beginPath()
        context.moveTo(ax, ay)
        context.lineTo(bx, cut)
        context.stroke()
        context.fillStyle = `rgba(207,190,57,${strength * .8})`
        context.fillRect(bx, cut - 1, 2, 2)
        context.fillRect(ax - 1, ay - 1, 2, 2)
      }
      // Fine transient fragments stay at the cut rather than coating the page.
      for (let index = 0; index < 75; index += 1) {
        const seed = tick * 19 + index * 17
        context.fillStyle = `rgba(157,165,147,${strength * .28})`
        context.fillRect(noise(seed) * width, cut - noise(seed + 4) * 8, 1 + noise(seed + 6) * 3, 1)
      }
    },
    dispose(): void {
      surfaces.forEach(surface => surface.style.removeProperty('clip-path'))
      context.clearRect(0, 0, width, height)
      delete root.dataset.windowExposure
    },
  }
}
