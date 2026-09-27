const SVG_NS = 'http://www.w3.org/2000/svg'
const REPORT = `PLATE 01 / INDUSTRIAL COMPOSITION
--------------------------------
REGION      : VALLEY IV
DRAWING     : STRUCTURAL OVERVIEW
REFERENCE   : CENTRAL LANDMARK

01 / VERTICAL STRUCTURE
     INTERSECTING SPINE
02 / LOWER CONCOURSE
     BRIDGES + SERVICE DECKS
03 / PRODUCTION DISTRICTS
     TERRACED BUILDING GROUPS
04 / TRANSPORT NETWORK
     SHARED DISTRIBUTION AXIS

SCALE / DIAGRAMMATIC
SERIES / INDUSTRIAL LANDSCAPE
--------------------------------
END OF RECORD / VALLEY IV`

export { REPORT }

export function sampleAscii(source: HTMLImageElement): string[] {
  const canvas = document.createElement('canvas')
  canvas.width = 96
  canvas.height = 60
  const context = canvas.getContext('2d', { willReadFrequently: true })
  if (!context) throw new Error('Wuling ASCII canvas could not initialize')
  context.drawImage(source, 0, 0, 96, 60)
  const pixels = context.getImageData(0, 0, 96, 60).data
  const ramp = ' -=+ox%#@'
  return Array.from({ length: 60 }, (_, row) => Array.from({ length: 96 }, (_, column) => {
    const offset = (row * 96 + column) * 4
    if (pixels[offset + 3] < 96) return ' '
    const luminance = pixels[offset] * .2126 + pixels[offset + 1] * .7152 + pixels[offset + 2] * .0722
    return ramp[Math.max(1, Math.min(8, Math.round(luminance / 255 * 8)))]
  }).join(''))
}

function pixelDissolveFrames(): Keyframe[] {
  const order = [0, 8, 2, 10, 12, 4, 14, 6, 3, 11, 1, 9, 15, 7, 13, 5]
  return Array.from({ length: 17 }, (_, stage) => {
    const cells = order.map((threshold, index) => threshold < stage
      ? `<rect x="${index % 4}" y="${Math.floor(index / 4)}" width="1" height="1" fill="white"/>` : '').join('')
    const svg = `<svg xmlns="http://www.w3.org/2000/svg" width="4" height="4">${cells}</svg>`
    return { maskImage: `url("data:image/svg+xml,${encodeURIComponent(svg)}")`, offset: stage / 16 }
  })
}

function animate(root: HTMLElement, selector: string, frames: Keyframe[], duration: number, delay: number): Animation {
  const node = root.querySelector(selector)
  if (!(node instanceof Element)) throw new Error(`Completion animation target missing: ${selector}`)
  return node.animate(frames, { duration, delay, easing: 'cubic-bezier(.18,.8,.24,1)', fill: 'both' })
}

function createAsciiLines(svg: SVGSVGElement, rows: string[]): SVGTextElement[] {
  const lines = rows.map((row, index) => {
    const line = document.createElementNS(SVG_NS, 'text')
    line.setAttribute('x', '0')
    line.setAttribute('y', String(index * 10 + 10))
    line.textContent = row
    return line
  })
  svg.replaceChildren(...lines)
  return lines
}

function updateAscii(lines: SVGTextElement[], rows: string[], elapsed: number): void {
  lines.forEach((line, row) => {
    const text = Array.from(rows[row], (glyph, column) => {
      if (glyph === ' ') return glyph
      const seed = (column * 37 + row * 71) % 379
      const age = elapsed - (420 + (59 - row) * 9 + seed)
      if (age < 0) return ' '
      if (age >= 320) return glyph
      return '-=+ox%#@'[(seed + Math.floor(age / 75)) % 8]
    }).join('')
    if (line.textContent !== text) line.textContent = text
  })
}

export function playCompletion(root: HTMLElement, mapId: 'map01' | 'map02', asciiRows: string[] | null, reducedMotion: boolean, dither: boolean): () => void {
  const animations: Animation[] = []
  let frame = 0
  const report = root.querySelector('.ec-report-text')
  if (!(report instanceof HTMLElement)) throw new Error('Valley completion report is missing')
  report.textContent = REPORT
  const svg = root.querySelector('.ec-ascii')
  if (mapId === 'map02' && (!(svg instanceof SVGSVGElement) || !asciiRows)) {
    throw new Error('Wuling ASCII artwork is missing')
  }
  const lines = mapId === 'map02' && svg instanceof SVGSVGElement && asciiRows
    ? createAsciiLines(svg, asciiRows) : []
  if (reducedMotion) return () => undefined
  const push = (selector: string, frames: Keyframe[], duration: number, delay: number): void => {
    animations.push(animate(root, selector, frames, duration, delay))
  }
  if (dither) {
    const scenery = root.querySelector(mapId === 'map02' ? '.recovery-matrix' : '.valley-assembly')
    if (!(scenery instanceof Element)) throw new Error('Completion scenery is missing')
    animations.push(scenery.animate(pixelDissolveFrames(), {
      duration: mapId === 'map02' ? 1400 : 760, easing: 'linear', fill: 'both',
    }))
  }
  if (mapId === 'map02' && asciiRows) {
    push('.recovery-landscape', [{ opacity: 0, transform: 'translateY(35px) scaleY(.8)' }, { opacity: .48, transform: 'translateY(0) scaleY(1)' }], 1150, 0)
    push('.ec-recovery-light', [{ opacity: 0, transform: 'translateY(70%)' }, { opacity: .85, offset: .5 }, { opacity: .2, transform: 'translateY(-65%)' }], 1600, 80)
    push('.ec-recovery-arrows', [{ opacity: 0, transform: 'translateY(35px)' }, { opacity: .65, offset: .4 }, { opacity: .16, transform: 'translateY(-45px)' }], 1500, 100)
    root.querySelectorAll('.recovery-block').forEach((node, index) => {
      animations.push(node.animate([{ opacity: 0, translate: '0 28px' }, { opacity: .85, translate: '0 0', offset: .28 }, { opacity: .18, translate: `0 ${-35 - index % 4 * 8}px` }], {
        duration: 1450, delay: 80 + Math.floor(index / 4) * 75, easing: 'cubic-bezier(.22,.7,.2,1)', fill: 'both',
      }))
    })
    push('.ec-content', [{ opacity: 0 }, { opacity: 1 }], 650, 480)
    push('.ec-check', [{ strokeDashoffset: 1 }, { strokeDashoffset: 0 }], 440, 650)
    push('.ec-title', [{ transform: 'translateY(105%)' }, { transform: 'translateY(0)' }], 620, 560)
    push('.ec-rule', [{ transform: 'scaleX(0)' }, { transform: 'scaleX(1)' }], 650, 640)
    push('.ec-details', [{ opacity: 0, transform: 'translateY(12px)' }, { opacity: 1, transform: 'translateY(0)' }], 460, 900)
    push('.ec-actions', [{ opacity: 0 }, { opacity: 1 }], 350, 1000)
    let started = 0
    let previousStep = -1
    const advance = (now: number): void => {
      if (started === 0) started = now
      const elapsed = now - started
      const step = Math.floor(elapsed / 55)
      if (step !== previousStep) updateAscii(lines, asciiRows, elapsed)
      previousStep = step
      if (elapsed < 1700) frame = requestAnimationFrame(advance)
      else lines.forEach((line, index) => { line.textContent = asciiRows[index] })
    }
    lines.forEach((line) => { line.textContent = '' })
    frame = requestAnimationFrame(advance)
  } else {
    report.textContent = ''
    push('.valley-infrastructure', [{ opacity: 0, clipPath: 'inset(0 50% 0 50%)' }, { opacity: 1, clipPath: 'inset(0 0 0 0)' }], 700, 80)
    push('.valley-landmark', [{ opacity: 0, transform: 'translateY(12px)' }, { opacity: 1, transform: 'translateY(0)' }], 480, 0)
    const origin = window.matchMedia('(max-width: 800px)').matches ? 54 : 22
    push('.valley-assembly', [{ clipPath: `inset(0 ${100 - origin}% 0 ${origin}%)` }, { clipPath: 'inset(0 0 0 0)' }], 720, 70)
    root.querySelectorAll('.ec-valley-scan').forEach((node, index) => {
      animations.push(node.animate([{ transform: 'translateX(0)', opacity: 0 }, { opacity: 1, offset: .1 }, { opacity: .8, offset: .75 }, { transform: `translateX(${index === 0 ? -100 : 100}%)`, opacity: 0 }], {
        duration: 720, delay: 70, easing: 'cubic-bezier(.25,.65,.25,1)', fill: 'both',
      }))
    })
    root.querySelectorAll('.valley-factory-row').forEach((node, index) => {
      animations.push(node.animate([{ translate: '0 20px', opacity: 0 }, { translate: '0 0', opacity: 1 }], {
        duration: 520, delay: 60 + index * 55, easing: 'cubic-bezier(.2,.7,.25,1)', fill: 'both',
      }))
    })
    push('.ec-valley-title', [{ opacity: 0, transform: 'translateX(14px)' }, { opacity: 1, transform: 'translateX(0)' }], 380, 180)
    push('.ec-content', [{ opacity: 0 }, { opacity: 1 }], 350, 130)
    push('.ec-check', [{ strokeDashoffset: 1 }, { strokeDashoffset: 0 }], 300, 300)
    push('.ec-title', [{ transform: 'translateY(105%)' }, { transform: 'translateY(0)' }], 430, 220)
    push('.ec-subtitle', [{ opacity: 0, transform: 'translateX(16px)' }, { opacity: 1, transform: 'translateX(0)' }], 300, 340)
    push('.ec-rule', [{ transform: 'scaleX(0)' }, { transform: 'scaleX(1)' }], 440, 250)
    push('.ec-details', [{ opacity: 0, transform: 'translateY(13px)' }, { opacity: 1, transform: 'translateY(0)' }], 280, 430)
    push('.ec-actions', [{ opacity: 0, transform: 'translateY(8px)' }, { opacity: 1, transform: 'translateY(0)' }], 260, 520)
    let started = 0
    const advance = (now: number): void => {
      if (started === 0) started = now
      const count = Math.floor(Math.max(0, Math.min(1, (now - started - 160) / 960)) * REPORT.length)
      report.textContent = REPORT.slice(0, count)
      if (count < REPORT.length) frame = requestAnimationFrame(advance)
    }
    frame = requestAnimationFrame(advance)
  }
  return () => {
    cancelAnimationFrame(frame)
    animations.forEach((animation) => animation.cancel())
  }
}
