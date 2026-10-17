import type { KeyboardEvent } from 'react'

export type CatalogAnchor = { id: string; offset: number; height: number }

export function readCatalogAnchor(container: HTMLElement): CatalogAnchor | null {
  const top = container.getBoundingClientRect().top
  const first = Array.from(container.querySelectorAll<HTMLElement>('[data-catalog-item]'))
    .find(item => item.getBoundingClientRect().bottom > top)
  if (!first) return null
  const bounds = first.getBoundingClientRect()
  return { id: first.id, offset: bounds.top - top, height: bounds.height }
}

export function restoreCatalogAnchor(container: HTMLElement, anchor: CatalogAnchor): void {
  const item = Array.from(container.querySelectorAll<HTMLElement>('[data-catalog-item]'))
    .find(candidate => candidate.id === anchor.id)
  if (!item) return
  const bounds = item.getBoundingClientRect()
  // Preserve the visible fraction when a partially clipped row changes height.
  const offset = anchor.offset < 0 && anchor.height > 0 ? anchor.offset * bounds.height / anchor.height : anchor.offset
  container.scrollTop += bounds.top - container.getBoundingClientRect().top - offset
}

/** Follow rendered rows so keyboard movement also works after responsive reflow. */
export function navigateCatalog(event: KeyboardEvent<HTMLElement>): void {
  if (event.altKey || event.ctrlKey || event.metaKey || event.shiftKey) return
  if (!['ArrowLeft', 'ArrowRight', 'ArrowUp', 'ArrowDown', 'Home', 'End'].includes(event.key)) return
  const target = event.target
  if (!(target instanceof HTMLButtonElement) || !target.hasAttribute('data-catalog-item')) return
  const items = Array.from(event.currentTarget.querySelectorAll<HTMLButtonElement>('button[data-catalog-item]:not(:disabled)'))
  const index = items.indexOf(target)
  if (index < 0) return
  let next = target
  if (event.key === 'Home') next = items[0]
  else if (event.key === 'End') next = items[items.length - 1]
  else if (event.key === 'ArrowLeft') next = items[Math.max(0, index - 1)]
  else if (event.key === 'ArrowRight') next = items[Math.min(items.length - 1, index + 1)]
  else {
    const current = target.getBoundingClientRect()
    const direction = event.key === 'ArrowDown' ? 1 : -1
    const candidates = items.map(item => ({ item, rect: item.getBoundingClientRect() }))
      .filter(({ rect }) => direction * (rect.top - current.top) > 2)
      .sort((a, b) => Math.abs(a.rect.top - current.top) - Math.abs(b.rect.top - current.top) || Math.abs(a.rect.left - current.left) - Math.abs(b.rect.left - current.left))
    next = candidates[0]?.item ?? target
  }
  event.preventDefault()
  next.focus({ preventScroll: true })
  next.scrollIntoView({ block: 'nearest', inline: 'nearest' })
}
