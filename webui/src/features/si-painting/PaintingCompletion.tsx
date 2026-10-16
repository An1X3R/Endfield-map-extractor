import { useLayoutEffect, useRef, useState } from 'react'
import { useReducedMotion } from 'motion/react'
import { COMPLETION_ART } from './artwork'
import { playPaintingCompletion } from './animation'
import { PAINTING_OUTCOMES } from './types'
import type { PaintingOutcome, PaintingResult } from './types'

const PREVIEWS: Record<PaintingOutcome, PaintingResult> = {
  success: { outcome: 'success', subtitle: '场景已导出 · 画境构建完成', primary: '场景结构、地形与纹理归档', secondary: '区域索引与场景清单', terminal: 'ARCHIVE / SI_PAINTING_REGION\nSCENE COMPILED — RECORD SEALED', selectionLabel: '4 SECTORS / PREVIEW' },
  partial: { outcome: 'partial', subtitle: '主体构建完成 · 有待处理项', primary: '场景结构、地形与纹理归档', secondary: '水体、灯光与特效重建', terminal: 'ARCHIVE / SI_PAINTING_REGION\nPARTIAL BUILD — REVIEW REQUIRED', selectionLabel: '4 SECTORS / PREVIEW' },
  failure: { outcome: 'failure', subtitle: '场景构建失败 · 请检查任务报告', primary: '保留已生成的阶段记录', secondary: '失败阶段与资源依赖', terminal: 'ARCHIVE / SI_PAINTING_REGION\nBUILD INTERRUPTED — REPORT REQUIRED', selectionLabel: '4 SECTORS / PREVIEW' },
  cancel: { outcome: 'cancel', subtitle: '导出已停止 · 返回地图继续选择', primary: '保留当前区域选择', secondary: '任务取消，未继续构建', terminal: 'ARCHIVE / SI_PAINTING_REGION\nCANCELLED — NO FURTHER BUILD', selectionLabel: '4 SECTORS / PREVIEW' },
}

export function PaintingCompletion({ result, onClose }: { result: PaintingResult | null; onClose: () => void }) {
  const dialogRef = useRef<HTMLDialogElement>(null)
  const reducedMotion = useReducedMotion() === true
  const [mode, setMode] = useState<PaintingOutcome>('success')
  const [replay, setReplay] = useState(0)
  const [dither, setDither] = useState(true)
  const current = result ?? PREVIEWS[mode]
  const copy = PAINTING_OUTCOMES[current.outcome]

  useLayoutEffect(() => {
    const dialog = dialogRef.current
    if (!dialog) return
    const previousFocus = document.activeElement
    dialog.showModal()
    return () => {
      dialog.close()
      if (previousFocus instanceof HTMLElement && previousFocus.isConnected) previousFocus.focus({ preventScroll: true })
    }
  }, [])
  useLayoutEffect(() => {
    const dialog = dialogRef.current
    if (!dialog) return
    return playPaintingCompletion(dialog, current.terminal, reducedMotion)
  }, [current.terminal, replay, reducedMotion])

  return <dialog ref={dialogRef} data-testid="si-completion-dialog" aria-labelledby="si-result-title" onCancel={event => { event.preventDefault(); onClose() }}>
    <div className="si-scroll-shell" id="si-scroll-shell">
      <div className="si-scroll-paper">
        <div className="si-paper-fibers" />
        <div className={`si-completion-art ${dither ? '' : 'si-no-dither'}`} dangerouslySetInnerHTML={{ __html: COMPLETION_ART }} />
        <div className="si-scroll-overline"><span>ATLAS / COMPOSER — 03</span><span>EXPORT / RESULT</span><button type="button" data-testid="si-close-completion" onClick={onClose} aria-label="关闭完成窗口">×</button></div>
        <div className="si-report">
          <div className="si-result-kicker"><span className="si-result-symbol">{copy.symbol}</span><div><b>{copy.kicker}</b><small>祀的画 / {current.selectionLabel}</small></div></div>
          <h2 id="si-result-title">{copy.title}</h2>
          <p className="si-result-sub">{current.subtitle}</p>
          <div className="si-report-rule"><i id="si-report-progress" /></div>
          <div className="si-report-record"><span>{current.outcome === 'failure' ? '记录' : current.outcome === 'cancel' ? '保留' : '已完成'}</span><p>{current.primary}</p></div>
          <div className="si-report-record"><span>{copy.label}</span><p>{current.secondary}</p></div>
          <p className="si-terminal-record" id="si-terminal-record" />
          <div className="si-result-stamp" aria-hidden="true">入<br />丹青</div>
          <div className="si-report-bottom"><div className="si-replay-controls"><button type="button" data-testid="si-replay-completion" onClick={() => setReplay(value => value + 1)}>重播动画 ↻</button><label><input type="checkbox" data-testid="si-dither-toggle" checked={dither} onChange={event => setDither(event.target.checked)} />像素肌理</label></div><button type="button" className="si-return-button" data-testid="si-return-map" onClick={onClose}>返回地图 <span>⟶</span></button></div>
        </div>
        <div className="si-art-caption">SI’S PAINTING / SECTOR TRANSFER <span>绘就山河</span></div>
      </div>
      <div className="si-scroll-rod si-rod-left" /><div className="si-scroll-rod si-rod-right" />
    </div>
    {!result && <div className="si-preview-controls"><span>完成状态预览</span><select aria-label="完成状态预览" data-testid="si-result-mode" value={mode} onChange={event => {
      const value = event.target.value
      if (!(value in PAINTING_OUTCOMES)) throw new Error(`Invalid painting preview outcome: ${value}`)
      setMode(value as PaintingOutcome)
    }}><option value="success">完成</option><option value="partial">完成 · 有待处理项</option><option value="failure">失败</option><option value="cancel">已取消</option></select><span>不执行提取任务</span></div>}
  </dialog>
}
