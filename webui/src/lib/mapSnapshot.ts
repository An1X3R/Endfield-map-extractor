export type CanvasMapSnapshot = {
  image: string
  left: number
  top: number
  width: number
  height: number
}

export function captureCanvasMap(canvas: HTMLCanvasElement): CanvasMapSnapshot {
  const rect = canvas.getBoundingClientRect()
  return { image: canvas.toDataURL('image/png'), left: rect.left, top: rect.top, width: rect.width, height: rect.height }
}

export function capturePaintingMap(stage: HTMLElement): SVGSVGElement {
  const source = stage.querySelector<SVGSVGElement>('.si-map-square > svg')
  if (!source) throw new Error('Cannot capture painting handoff: map SVG is missing')
  const bounds = stage.getBoundingClientRect()
  const frame = source.getBoundingClientRect()
  const snapshot = document.createElementNS('http://www.w3.org/2000/svg', 'svg')
  snapshot.setAttribute('viewBox', `0 0 ${bounds.width} ${bounds.height}`)
  snapshot.setAttribute('preserveAspectRatio', 'none')
  snapshot.style.cssText = 'display:block;width:100%;height:100%;background:#191c18'
  const artwork = source.cloneNode(true) as SVGSVGElement
  artwork.setAttribute('x', String(frame.left - bounds.left))
  artwork.setAttribute('y', String(frame.top - bounds.top))
  artwork.setAttribute('width', String(frame.width))
  artwork.setAttribute('height', String(frame.height))
  // Isolate copied definitions from the still-mounted source SVG.
  const prefix = `handoff-${crypto.randomUUID()}-`
  const nodes = [artwork, ...artwork.querySelectorAll('*')]
  const ids = new Set(nodes.filter(node => node.id).map(node => node.id))
  for (const node of nodes) {
    for (const attribute of Array.from(node.attributes)) {
      let value = attribute.value
      value = value.replace(/url\(\s*(['"]?)#([^)'"\s]+)\1\s*\)/g, (reference: string, quote: string, id: string) =>
        ids.has(id) ? `url(${quote}#${prefix}${id}${quote})` : reference)
      if ((attribute.localName === 'href') && value.startsWith('#') && ids.has(value.slice(1))) value = `#${prefix}${value.slice(1)}`
      if (attribute.name === 'aria-labelledby' || attribute.name === 'aria-describedby') value = value.split(/\s+/).map(id => ids.has(id) ? prefix + id : id).join(' ')
      if (attribute.name === 'id') value = prefix + value
      node.setAttributeNS(attribute.namespaceURI, attribute.name, value)
    }
  }
  snapshot.append(artwork)
  return snapshot
}
