import { useEffect, useRef, useState } from 'react'
import { describeAppearance } from './appearance'
import type { CharacterAppearanceOptions, CharacterAppearanceReport, CharacterMaterialOptions } from './types'
import { describeMaterials } from './materials'
import { createScan } from './scan'
import type { OperatorPortrait } from './portraits'
import type { ScanFrameDetail } from './scan'
import { createCompletionMotion, OPERATOR_COMPLETION_DURATION } from './completionMotion'

export type OperatorReport = {
  assetType?: 'model' | 'illustration'
  illustrationIds?: string[]
  appearanceOptions?: CharacterAppearanceOptions
  appearanceReport?: CharacterAppearanceReport
  materialOptions?: CharacterMaterialOptions
  portrait: OperatorPortrait | null
  id: string
  state: 'completed' | 'partial' | 'failed' | 'cancelled'
  entity: string
  output: string | null
  files: Array<{ relative_path: string; role: string; bytes: number; sha256: string }>
  pending: Array<{ code: string; message: string }>
  error: string | null
  development: boolean
}

type Props = {
  report: OperatorReport
  open: boolean
  reduced: boolean
  onClose: () => void
  onCopy: (text: string, confirmation: string) => void
}

/** Keep the report available from the first frame while the portrait registers. */
export function OperatorCompletion({ report, open, reduced, onClose, onCopy }: Props) {
  const dialog = useRef<HTMLDialogElement>(null)
  const canvas = useRef<HTMLCanvasElement>(null)
  const scan = useRef<ReturnType<typeof createScan> | null>(null)
  const choreography = useRef<ReturnType<typeof createCompletionMotion> | null>(null)
  const exitMotion = useRef<Animation | null>(null)
  const previousReport = useRef<string | null>(null)
  const [frame, setFrame] = useState({ progress: 0, printProgress: 0 })
  const [duration, setDuration] = useState(OPERATOR_COMPLETION_DURATION)
  const [replay, setReplay] = useState(0)
  const success = report.state === 'completed' || report.state === 'partial'
  const partial = report.state === 'partial'
  const illustration = report.assetType === 'illustration'

  useEffect(() => {
    const node = canvas.current
    if (!node) return
    scan.current = createScan(node, report.portrait?.url ?? null)
    const update = (event: Event): void => {
      const detail = (event as CustomEvent<ScanFrameDetail>).detail
      choreography.current?.seek(detail.progress)
      setFrame({ progress: detail.progress, printProgress: choreography.current?.exposure() ?? detail.printProgress })
    }
    node.addEventListener('scanframe', update)
    return () => {
      exitMotion.current?.cancel()
      exitMotion.current = null
      node.removeEventListener('scanframe', update)
      scan.current?.dispose()
      scan.current = null
    }
  }, [report.portrait?.url])

  useEffect(() => {
    const node = dialog.current
    if (!node) return
    if (open) {
      if (!node.open) node.showModal()
      if (success) {
        const playbackDuration = previousReport.current === report.id ? duration : OPERATOR_COMPLETION_DURATION
        previousReport.current = report.id
        if (playbackDuration !== duration) setDuration(playbackDuration)
        choreography.current = createCompletionMotion(node)
        scan.current?.play(playbackDuration, reduced, partial)
      }
      else { scan.current?.reset(); setFrame({ progress: 0, printProgress: 0 }) }
    } else {
      exitMotion.current?.cancel()
      exitMotion.current = null
      node.close()
      setDuration(OPERATOR_COMPLETION_DURATION)
      scan.current?.reset()
    }
    return () => {
      choreography.current?.dispose()
      choreography.current = null
    }
  }, [open, report.id, report.portrait?.url, success, partial, reduced, duration, replay])

  const close = (): void => {
    const node = dialog.current
    if (!node || reduced) { onClose(); return }
    if (exitMotion.current) return
    exitMotion.current = node.animate([{ opacity: 1, transform: 'translateY(0)' }, { opacity: 0, transform: 'translateY(6px)' }], { duration: 140, easing: 'ease-in', fill: 'forwards' })
    exitMotion.current.onfinish = onClose
  }

  const title = illustration
    ? partial ? '部分立绘完成' : report.state === 'completed' ? '角色立绘提取完成' : report.state === 'failed' ? '提取未完成' : '立绘提取已取消'
    : partial ? '导出完成，有待处理项' : report.state === 'completed' ? '导出完成' : report.state === 'failed' ? '导出失败' : '任务已取消'
  const completionSubtitle = illustration
    ? partial ? `已导出 ${report.files.length} 张 · 仍有立绘未完成` : success ? '原尺寸无损 PNG 已导出' : report.error ?? '后端已确认任务取消'
    : partial ? '产物已生成 · 有待处理项；数量不等于缺失资产数量' : success ? '导出完成 · 产物已就绪' : report.error ?? '后端已确认任务取消'
  const outputLabel = illustration ? 'OUTPUT // RGBA TRANSPARENT PNG' : 'OUTPUT / BLENDER'
  return (
    <dialog ref={dialog} id="completion-dialog" data-testid="operator-completion" className={`${partial ? 'partial ' : ''}${illustration ? 'illustration-completion' : ''}`} aria-labelledby="completion-title" onCancel={event => { event.preventDefault(); close() }}>
      <header className="completion-top"><span>{illustration ? 'OPERATOR ARCHIVE // ILLUSTRATION EXPORT' : 'ENDFIELD / ENTITY FABRICATION'}</span><span>EXPORT RESULT <i>04</i></span><button id="completion-close" data-testid="operator-completion-close" aria-label="关闭完成动画" onClick={close}>×</button></header>
      <canvas className="operator-window-rays" aria-hidden="true" /><div className="completion-layout">
        <section className="sculpture-stage"><div className="operator-plate-atmosphere" aria-hidden="true" /><svg className="operator-plate-grid" viewBox="0 0 472 570" preserveAspectRatio="none" aria-hidden="true"><g fill="none" stroke="currentColor"><path pathLength="1" d="M45 100V62h95M332 62h95v38M427 456v38h-95M140 494H45v-38M27 184h35m-35 96h20m-20 96h35M410 184h35m-20 96h20m-35 96h35" /><path pathLength="1" d="M78 72v414M394 72v414M72 128h328M72 440h328" /></g></svg><svg className="operator-registration-lock" viewBox="0 0 384 444" preserveAspectRatio="none" aria-hidden="true"><path d="M1 25V1h24M359 1h24v24M383 419v24h-24M25 443H1v-24" fill="none" stroke="currentColor" vectorEffect="non-scaling-stroke" /></svg><div className="sculpture-word" aria-hidden="true">ENTITY<br />RECORD.</div><canvas ref={canvas} id="scan-canvas" data-testid="operator-scan-canvas" data-character-id={report.portrait?.characterId ?? "unassigned"} aria-label={report.portrait ? `${report.portrait.name}头像扫描显影` : "实体档案扫描显影，暂无角色头像"} /><div className="sculpture-footer"><span>01 / PORTRAIT REGISTRATION</span><span id="scan-motion-status">{success ? frame.progress >= 1 ? 'PLATE / REGISTERED' : `EXPOSURE / ${Math.round(frame.printProgress * 100)}%` : report.state.toUpperCase()}</span></div></section>
        <section className="completion-report"><div className="operator-report-field" aria-hidden="true" />
          <div className="report-decoration" aria-hidden="true"><span className="report-ghost-number">04</span><svg viewBox="0 0 680 610" preserveAspectRatio="xMaxYMid slice"><g fill="none" stroke="currentColor"><path pathLength="1" d="M472 40h150v154M622 244v292H390M344 536H42M558 40v496M42 488h580" /><path d="M440 66h154v354H440zM456 82h122v322H456z" strokeDasharray="3 9" /><circle pathLength="1" cx="574" cy="438" r="134" /><circle pathLength="1" cx="574" cy="438" r="108" /><path pathLength="1" d="M412 438h268M574 277v290M451 349l19 19m194 194-19-19M478 531l-20 20" /><path pathLength="1" d="M632 84h14m-14 12h8m-8 12h8m-8 12h14m-14 12h8m-8 12h8m-8 12h14M72 549v14m12-14v8m12-8v8m12-8v14m12-14v8m12-8v8m12-8v14" /></g></svg><span className="report-registration">ENTITY / ARCHIVE<br />REGISTRATION PLATE — 04</span><span className="report-edge-code">PORTRAIT · MATERIAL · MOTION</span></div>
          <div className="completion-kicker"><span className="completion-light" /> {illustration ? 'OPERATOR ARCHIVE / ILLUSTRATION EXPORT' : partial ? 'ASSET / PARTIAL' : success ? 'ASSET / COMPILED' : `TASK / ${report.state.toUpperCase()}`}</div><p id="completion-entity">{report.entity}</p><div className="operator-title-mask"><h1 id="completion-title">{title}</h1></div><p className="completion-subtitle">{completionSubtitle}</p><div className="completion-rule"><i id="completion-progress" style={{ width: `${frame.printProgress * 100}%` }} /></div>
          <div className="completion-summary"><span>{outputLabel}</span><strong id="completion-path">{report.output ?? '未返回产物目录'}</strong><p id="completion-pending">{illustration ? `ORIGINAL RESOLUTION // 原尺寸无损导出 · 已导出立绘数量：${report.files.length} 张` : partial ? `待处理 ${report.pending.length} 项 · 详情保留在档案报告中` : report.development ? '开发样例 · 不生成真实产物' : `${report.files.length} 项已生成文件`}</p>{illustration && report.illustrationIds && <p data-testid="operator-completion-illustration-ids">关联 ID：{report.illustrationIds.join(' · ')}</p>}{!report.development && report.materialOptions && report.output && <p data-testid="operator-completion-materials">实际材质：{describeMaterials(report.materialOptions)}</p>}{!report.development && report.appearanceReport && <p data-testid="operator-completion-appearance">{describeAppearance(report.appearanceReport)} · 粒子为固定快照</p>}</div>
          {illustration && report.files.length > 0 && <ul className="illustration-file-list" data-testid="operator-completion-illustration-files">{report.files.map(file => <li key={file.relative_path}><code>{file.relative_path}</code> · {file.bytes.toLocaleString()} B</li>)}</ul>}{illustration && report.pending.length > 0 && <ul className="illustration-pending-list" data-testid="operator-completion-illustration-pending">{report.pending.map((item, index) => <li key={`${item.code}-${index}`}><b>{item.code}</b> · {item.message}</li>)}</ul>}
          <div className="completion-controls">{success && <><button data-testid="operator-replay" onClick={() => { setDuration(OPERATOR_COMPLETION_DURATION); setReplay(value => value + 1) }}>重播动画 ↻</button><button data-testid="operator-slow" onClick={() => { setDuration(6500); setReplay(value => value + 1) }}>慢速观察</button></>}{report.output && <button data-testid="operator-modal-copy" onClick={() => onCopy(report.output!, '产物目录已复制')}>{report.development ? '复制示例路径' : '复制产物目录'}</button>}{partial && <button data-testid="operator-modal-diagnostics" onClick={() => onCopy(report.pending.map(item => `${item.code}: ${item.message}`).join('\n'), '完整诊断已复制')}>复制完整诊断</button>}</div>
          <button className="completion-return" data-testid="operator-completion-return" onClick={close}>{illustration ? '返回角色档案' : '返回角色档案'} <span>↗</span></button><small>{report.development ? 'DEV PREVIEW / ' : ''}{report.portrait ? '角色档案 · 扫描显影' : '实体档案 · 暂无角色头像'}</small>
        </section>
      </div>
    </dialog>
  )
}

