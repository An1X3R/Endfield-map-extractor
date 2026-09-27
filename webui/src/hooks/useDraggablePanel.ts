import { useCallback, useEffect, useRef, useState } from 'react'
import type {
  KeyboardEvent as ReactKeyboardEvent,
  PointerEvent as ReactPointerEvent,
  RefObject,
} from 'react'

type PanelOffset = {
  x: number
  y: number
}

type PanelDrag = {
  pointerId: number
  pointerX: number
  pointerY: number
  offsetX: number
  offsetY: number
}

type DraggablePanelApi = {
  panelRef: RefObject<HTMLElement | null>
  headerRef: RefObject<HTMLElement | null>
  isDragging: boolean
  onPointerDown: (event: ReactPointerEvent<HTMLElement>) => void
  onPointerMove: (event: ReactPointerEvent<HTMLElement>) => void
  onPointerUp: (event: ReactPointerEvent<HTMLElement>) => void
  onPointerCancel: (event: ReactPointerEvent<HTMLElement>) => void
  onLostPointerCapture: (event: ReactPointerEvent<HTMLElement>) => void
  onKeyDown: (event: ReactKeyboardEvent<HTMLElement>) => void
  reset: () => void
}

const clamp = (value: number, min: number, max: number) =>
  Math.min(max, Math.max(min, value))

export function useDraggablePanel(
  workspaceRef: RefObject<HTMLElement | null>,
  disabled: boolean,
): DraggablePanelApi {
  const panelRef = useRef<HTMLElement | null>(null)
  const headerRef = useRef<HTMLElement | null>(null)
  const offsetRef = useRef<PanelOffset>({ x: 0, y: 0 })
  const dragRef = useRef<PanelDrag | null>(null)
  const [isDragging, setIsDragging] = useState(false)

  const movePanel = useCallback((x: number, y: number) => {
    const workspace = workspaceRef.current
    const panel = panelRef.current
    if (!workspace || !panel) return

    const workspaceBounds = workspace.getBoundingClientRect()
    const panelBounds = panel.getBoundingClientRect()
    const baseLeft = panelBounds.left - workspaceBounds.left - offsetRef.current.x
    const baseTop = panelBounds.top - workspaceBounds.top - offsetRef.current.y
    const nextOffset = {
      x: clamp(x, -baseLeft, Math.max(-baseLeft, workspaceBounds.width - panelBounds.width - baseLeft)),
      y: clamp(y, -baseTop, Math.max(-baseTop, workspaceBounds.height - 44 - panelBounds.height - baseTop)),
    }
    offsetRef.current = nextOffset
    panel.style.transform = `translate(${nextOffset.x}px, ${nextOffset.y}px)`
  }, [workspaceRef])

  const reset = useCallback(() => {
    dragRef.current = null
    offsetRef.current = { x: 0, y: 0 }
    panelRef.current?.style.removeProperty('transform')
    setIsDragging(false)
  }, [])

  const onPointerDown = useCallback((event: ReactPointerEvent<HTMLElement>) => {
    if (disabled || event.button !== 0 || window.innerWidth <= 800) return
    const target = event.target
    if (target instanceof Element && target.closest('button, input, select, textarea, summary, a, label, [contenteditable="true"], [data-no-panel-drag]')) return

    const header = headerRef.current
    if (!header) return
    event.preventDefault()
    header.focus({ preventScroll: true })
    header.setPointerCapture(event.pointerId)
    dragRef.current = {
      pointerId: event.pointerId,
      pointerX: event.clientX,
      pointerY: event.clientY,
      offsetX: offsetRef.current.x,
      offsetY: offsetRef.current.y,
    }
    setIsDragging(true)
  }, [disabled])

  const onPointerMove = useCallback((event: ReactPointerEvent<HTMLElement>) => {
    const drag = dragRef.current
    if (!drag || drag.pointerId !== event.pointerId || disabled) return
    movePanel(drag.offsetX + event.clientX - drag.pointerX, drag.offsetY + event.clientY - drag.pointerY)
  }, [disabled, movePanel])

  const finishDrag = useCallback((event: ReactPointerEvent<HTMLElement>) => {
    const drag = dragRef.current
    if (!drag || drag.pointerId !== event.pointerId) return
    dragRef.current = null
    setIsDragging(false)
    const header = headerRef.current
    if (header?.hasPointerCapture(event.pointerId)) header.releasePointerCapture(event.pointerId)
  }, [])

  const onKeyDown = useCallback((event: ReactKeyboardEvent<HTMLElement>) => {
    if (disabled || event.target !== headerRef.current || window.innerWidth <= 800) return
    const distance = event.shiftKey ? 32 : 8
    const directions: Record<string, PanelOffset> = {
      ArrowLeft: { x: -distance, y: 0 },
      ArrowRight: { x: distance, y: 0 },
      ArrowUp: { x: 0, y: -distance },
      ArrowDown: { x: 0, y: distance },
    }
    const delta = directions[event.key]
    if (delta) {
      event.preventDefault()
      movePanel(offsetRef.current.x + delta.x, offsetRef.current.y + delta.y)
    }
    if (event.key === 'Home') {
      event.preventDefault()
      reset()
    }
  }, [disabled, movePanel, reset])

  useEffect(() => {
    const handleResize = () => {
      if (window.innerWidth <= 800) reset()
      else movePanel(offsetRef.current.x, offsetRef.current.y)
    }
    window.addEventListener('resize', handleResize)
    if (disabled) reset()
    return () => window.removeEventListener('resize', handleResize)
  }, [disabled, movePanel, reset])

  return {
    panelRef,
    headerRef,
    isDragging,
    onPointerDown,
    onPointerMove,
    onPointerUp: finishDrag,
    onPointerCancel: finishDrag,
    onLostPointerCapture: finishDrag,
    onKeyDown,
    reset,
  }
}
