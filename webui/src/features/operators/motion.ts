const BAYER = [0, 8, 2, 10, 12, 4, 14, 6, 3, 11, 1, 9, 15, 7, 13, 5]

function ditherMask(level: number): string {
  const squares = BAYER.map((value, index) => value < level ? `<rect x="${index % 4 * 2}" y="${Math.floor(index / 4) * 2}" width="2" height="2" fill="white"/>` : '').join('')
  return `url("data:image/svg+xml,${encodeURIComponent(`<svg xmlns="http://www.w3.org/2000/svg" width="8" height="8">${squares}</svg>`)}")`
}

/** Reveal independent regions and cancel the entire sequence on a new selection. */
export function revealOperators(nodes: Array<HTMLElement | null>, delays: number[], instant: boolean): () => void {
  const frames: number[] = []
  nodes.forEach((node, index) => {
    if (!node) return
    node.style.maskImage = ''
    if (instant) return
    const start = performance.now() + delays[index]
    const tick = (time: number): void => {
      const level = Math.max(0, Math.min(16, Math.floor((time - start) / 100 * 16)))
      if (level === 16) { node.style.maskImage = ''; return }
      node.style.maskImage = ditherMask(level)
      frames[index] = requestAnimationFrame(tick)
    }
    tick(performance.now())
  })
  return () => {
    frames.forEach(cancelAnimationFrame)
    nodes.forEach(node => node?.style.removeProperty('mask-image'))
  }
}
