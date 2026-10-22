import { useId, useLayoutEffect, useRef, useState } from 'react'
import './weapon-completion.css'

export type WeaponCompletionArtifact = {
  kind: 'blend' | 'preparation_receipt' | 'build_receipt' | 'reopen_audit'
  path: string
}

export type WeaponCompletionReport = {
  state: 'completed' | 'partial' | 'failed' | 'cancelled'
  weaponId: string | null
  name: string
  itemId: string
  variant: 'base' | 'refined_candidate'
  rarity: number | null
  variantSemantics: 'source_base' | 'unverified_candidate' | null
  sourceResource: string | null
  outputDirectory: string | null
  reopenAudit: 'passed' | 'receipt_missing' | 'not_generated'
  artifacts: WeaponCompletionArtifact[]
  pending: string[]
  limitations: string[]
  error: string | null
  development: boolean
}

type Props = {
  open: boolean
  report: WeaponCompletionReport
  reduced: boolean
  onClose: () => void
}

const FORGE_DURATION_MS = 1240
const SPARK_DURATION_MS = 420
const clamp01 = (value: number): number => Math.max(0, Math.min(1, value))
const segment = (value: number, start: number, end: number): number => clamp01((value - start) / (end - start))
const easeOut = (value: number): number => 1 - Math.pow(1 - clamp01(value), 3)
const easeIn = (value: number): number => Math.pow(clamp01(value), 2)
const damped = (value: number): number => {
  const progress = clamp01(value)
  return (1 - (1 + 7 * progress) * Math.exp(-7 * progress)) / (1 - 8 * Math.exp(-7))
}

const phaseAt = (elapsed: number): string => {
  if (elapsed < 290) return 'pour'
  if (elapsed < 490) return 'impact'
  if (elapsed < 740) return 'reshape'
  if (elapsed < 1140) return 'print'
  return 'settle'
}

const titleFor = (report: WeaponCompletionReport): string => {
  if (report.state === 'completed') return '武器资产构建完成'
  if (report.state === 'partial') return '部分武器资产完成'
  if (report.state === 'failed') return '武器提取失败'
  return '武器任务已取消'
}

const stateLabel = (state: WeaponCompletionReport['state']): string => state.toUpperCase()

const reopenAuditStatusFor = (report: WeaponCompletionReport): string => {
  if (report.development) return 'DEV PREVIEW'
  if (report.reopenAudit === 'passed') return 'REOPEN AUDIT / PASS'
  if (report.reopenAudit === 'receipt_missing') return 'NO RECEIPT PATH'
  return '未生成'
}

const artifactLabels: Record<WeaponCompletionArtifact['kind'], string> = {
  blend: 'BLENDER FILE',
  preparation_receipt: 'PREPARATION RECEIPT',
  build_receipt: 'BUILD RECEIPT',
  reopen_audit: 'REOPEN AUDIT RECEIPT',
}

export function WeaponCompletion({ open, report, reduced, onClose }: Props) {
  const dialogRef = useRef<HTMLDialogElement>(null)
  const [replay, setReplay] = useState(0)
  const [slow, setSlow] = useState(false)
  const dieMaskId = `weapon-die-${useId().replace(/[^a-zA-Z0-9_-]/g, '')}`
  const successful = report.state === 'completed' || report.state === 'partial'
  const rawRarity = report.rarity ?? 0
  const reportSignature = `${report.state}\u0000${report.weaponId}\u0000${report.name}\u0000${report.itemId}\u0000${report.variant}\u0000${report.variantSemantics}\u0000${report.rarity}\u0000${report.sourceResource}\u0000${report.outputDirectory ?? ''}\u0000${report.reopenAudit}\u0000${report.artifacts.map(artifact => `${artifact.kind}:${artifact.path}`).join('\u0000')}\u0000${report.pending.join('\u0000')}\u0000${report.limitations.join('\u0000')}\u0000${report.error ?? ''}\u0000${report.development}`
  const title = titleFor(report)

  useLayoutEffect(() => {
    const dialog = dialogRef.current
    if (!open || !dialog) return
    if (!dialog.open) dialog.showModal()
    return () => {
      if (dialog.open) dialog.close()
    }
  }, [open])

  useLayoutEffect(() => {
    if (!open) return
    const root = dialogRef.current
    if (!root) return

    let animationFrame = 0
    const printableRows = Array.from(root.querySelectorAll<HTMLElement>('[data-print-row]'))
    const clearFrameStyles = (): void => {
      root.style.removeProperty('--forge-progress')
      root.style.removeProperty('--window-reveal')
      root.style.removeProperty('--pool-fill')
      root.style.removeProperty('--stream-strength')
      root.style.removeProperty('--clamp-lock')
      root.style.removeProperty('--die-reveal')
      root.style.removeProperty('--cooling-progress')
      root.style.removeProperty('--hammer-drop')
      root.style.removeProperty('--hammer-visibility')
      root.style.removeProperty('--hammer-morph')
      root.style.removeProperty('--title-reveal')
      root.style.removeProperty('--spark-age')
      root.style.removeProperty('--spark-visibility')
      root.style.removeProperty('--report-reveal')
      root.style.removeProperty('--engineering-print')
      root.style.removeProperty('--pool-impact')
      root.style.removeProperty('--forge-recoil')
      root.style.removeProperty('--plate-lift')
      printableRows.forEach(row => {
        row.style.removeProperty('clip-path')
        row.style.removeProperty('opacity')
        row.style.removeProperty('transform')
      })
    }

    const clearPendingFrame = (): void => {
      root.dataset.forgePhase = 'pending'
      root.dataset.forgeProgress = '0.0000'
      root.dataset.forgeActive = 'true'
      root.dataset.actionsReady = 'false'
      root.dataset.printReady = 'false'
      const actions = root.querySelector<HTMLElement>('.weapon-completion-actions-buttons')
      if (actions) actions.inert = true
      clearFrameStyles()
    }

    if (!successful) {
      root.dataset.forgePhase = 'report'
      root.dataset.forgeProgress = '0.0000'
      root.dataset.forgeActive = 'false'
      root.dataset.actionsReady = 'true'
      root.dataset.printReady = 'true'
      const actions = root.querySelector<HTMLElement>('.weapon-completion-actions-buttons')
      if (actions) actions.inert = false
      clearFrameStyles()
      return
    }

    root.dataset.forgeActive = 'true'
    const impression = root.querySelector<HTMLElement>('.weapon-completion-impression')
    const hammer = root.querySelector<HTMLElement>('.weapon-completion-hammer')
    const stream = root.querySelector<HTMLElement>('.weapon-completion-stream')
    const stage = root.querySelector<HTMLElement>('.weapon-completion-stage')
    const topClamp = root.querySelector<HTMLElement>('.weapon-completion-clamp--top')
    const bottomClamp = root.querySelector<HTMLElement>('.weapon-completion-clamp--bottom')
    const sparks = root.querySelector<SVGSVGElement>('.weapon-completion-sparks')
    const actions = root.querySelector<HTMLElement>('.weapon-completion-actions-buttons')
    const particles = Array.from(root.querySelectorAll<SVGLineElement>('.weapon-completion-sparks line'))
    if (!impression || !hammer || !stream || !stage || !topClamp || !bottomClamp || !sparks || !actions) {
      throw new Error('Weapon completion forge elements are incomplete')
    }
    const targetWidth = Math.max(56, impression.clientWidth)
    const entranceDistance = impression.getBoundingClientRect().bottom - root.getBoundingClientRect().top + 16
    const stageHeight = stage.clientHeight

    const applyFrame = (elapsed: number): void => {
      const progress = clamp01(elapsed / FORGE_DURATION_MS)
      const poolFill = damped(segment(elapsed, 20, 560))
      const streamStrength = easeOut(segment(elapsed, 0, 65)) * (1 - damped(segment(elapsed, 370, 600)))
      const clampLock = damped(segment(elapsed, 330, 610))
      const dieReveal = damped(segment(elapsed, 200, 610))
      const cooling = damped(segment(elapsed, 380, 860))
      const hammerDrop = easeIn(segment(elapsed, 260, 490))
      const hammerMorph = damped(segment(elapsed, 490, 870))
      const titleReveal = damped(segment(elapsed, 600, 925))
      const sparkAge = segment(elapsed, 490, 490 + SPARK_DURATION_MS)
      const sparkVisibility = elapsed >= 490 && elapsed < 490 + SPARK_DURATION_MS ? 1 : 0
      const windowReveal = easeOut(segment(elapsed, 0, 90))
      const reportReveal = damped(segment(elapsed, 610, 900))
      const engineeringPrint = damped(segment(elapsed, 170, 920))
      const settling = Math.exp(-6 * segment(elapsed, 490, 1160))
      const recoil = elapsed >= 490 ? Math.sin(segment(elapsed, 490, 780) * Math.PI * 2) * 6 * settling : 0

      root.dataset.forgePhase = phaseAt(elapsed)
      root.dataset.forgeProgress = progress.toFixed(4)
      root.dataset.actionsReady = elapsed >= 900 ? 'true' : 'false'
      root.dataset.printReady = elapsed >= FORGE_DURATION_MS ? 'true' : 'false'
      actions.inert = elapsed < 900
      root.style.setProperty('--forge-progress', progress.toFixed(4))
      root.style.setProperty('--window-reveal', windowReveal.toFixed(4))
      root.style.setProperty('--pool-fill', poolFill.toFixed(4))
      root.style.setProperty('--stream-strength', streamStrength.toFixed(4))
      root.style.setProperty('--clamp-lock', clampLock.toFixed(4))
      root.style.setProperty('--die-reveal', dieReveal.toFixed(4))
      root.style.setProperty('--cooling-progress', `${(cooling * 100).toFixed(2)}%`)
      root.style.setProperty('--hammer-drop', hammerDrop.toFixed(4))
      root.style.setProperty('--hammer-visibility', '1')
      root.style.setProperty('--hammer-morph', hammerMorph.toFixed(4))
      root.style.setProperty('--title-reveal', `${(titleReveal * 100).toFixed(2)}%`)
      root.style.setProperty('--spark-age', sparkAge.toFixed(4))
      root.style.setProperty('--spark-visibility', sparkVisibility.toFixed(4))
      root.style.setProperty('--report-reveal', reportReveal.toFixed(4))
      root.style.setProperty('--engineering-print', engineeringPrint.toFixed(4))
      root.style.setProperty('--pool-impact', (streamStrength * .8 + Math.abs(recoil) * .08).toFixed(4))
      root.style.setProperty('--forge-recoil', `${recoil.toFixed(3)}px`)
      root.style.setProperty('--plate-lift', `${((1 - dieReveal) * 36 - recoil).toFixed(3)}px`)

      const hammerHeight = 180 - 126 * hammerMorph
      hammer.style.width = hammerMorph === 1 ? '100%' : `${56 + (targetWidth - 56) * hammerMorph}px`
      hammer.style.height = `${hammerHeight}px`
      hammer.style.transform = `translateY(${(1 - hammerDrop) * -entranceDistance - recoil}px)`
      topClamp.style.transform = `translateY(${(1 - clampLock) * -52 + recoil}px)`
      bottomClamp.style.transform = `translateY(${(1 - clampLock) * 40 + recoil}px)`
      stream.style.height = `${Math.max(0, stageHeight * (1 - .4 * poolFill))}px`
      sparks.style.opacity = `${sparkVisibility * Math.pow(1 - sparkAge, .8)}`
      particles.forEach((particle, index) => {
        const angle = -Math.PI + index / particles.length * Math.PI
        const speed = 70 + index % 5 * 27
        const flight = easeOut(sparkAge)
        const x = Math.cos(angle) * speed * flight
        const y = Math.sin(angle) * speed * flight + sparkAge * sparkAge * 94
        particle.setAttribute('x1', `${x}`)
        particle.setAttribute('y1', `${y}`)
        particle.setAttribute('x2', `${x - Math.cos(angle) * (3 + index % 4)}`)
        particle.setAttribute('y2', `${y - Math.sin(angle) * (3 + index % 4)}`)
      })

      const availablePrintTime = 1130 - 680 - 120
      const printStep = Math.min(32, availablePrintTime / Math.max(1, printableRows.length - 1))
      printableRows.forEach((row, index) => {
        const rowProgress = damped(segment(elapsed, 680 + index * printStep, 800 + index * printStep))
        row.style.clipPath = `inset(0 ${(1 - rowProgress) * 100}% 0 0)`
        row.style.opacity = `${rowProgress}`
        row.style.transform = `translateY(${(1 - rowProgress) * 9}px)`
      })
    }

    if (reduced) {
      applyFrame(FORGE_DURATION_MS)
      return
    }

    applyFrame(0)
    const startedAt = performance.now()
    const tick = (now: number): void => {
      const elapsed = Math.min(FORGE_DURATION_MS, (now - startedAt) * (slow ? .35 : 1))
      applyFrame(elapsed)
      if (elapsed < FORGE_DURATION_MS) animationFrame = window.requestAnimationFrame(tick)
    }
    animationFrame = window.requestAnimationFrame(tick)
    return () => {
      window.cancelAnimationFrame(animationFrame)
      clearPendingFrame()
    }
  }, [open, reduced, replay, reportSignature, successful, slow])

  if (!open) return null

  const sparkLines = Array.from({ length: 20 }, (_, index) => {
    const angle = index * Math.PI * 2 / 20
    const distance = 25 + index % 4 * 8
    const x = Math.cos(angle) * distance
    const y = Math.sin(angle) * distance
    return <line key={index} x1="0" y1="0" x2={x.toFixed(2)} y2={y.toFixed(2)} />
  })

  return (
    <dialog
      ref={dialogRef}
      className={`weapon-completion weapon-completion--${report.state}`}
      data-testid="weapon-completion"
      data-state={report.state}
      data-forge-phase={successful ? 'pending' : 'report'}
      data-forge-active={successful ? 'true' : 'false'}
      data-forge-progress="0.0000"
      data-actions-ready={successful ? 'false' : 'true'}
      data-print-ready={successful ? 'false' : 'true'}
      aria-labelledby="weapon-completion-title"
      onCancel={event => {
        event.preventDefault()
        onClose()
      }}
    >
      <div className="weapon-completion-frame">
        <header className="weapon-completion-header">
          <span className="weapon-completion-wordmark"><i aria-hidden="true" />ENDFIELD / WEAPON ASSET PIPELINE</span>
          <span className="weapon-completion-header-state">EXPORT RESULT <b>04</b></span>
          <button type="button" data-testid="weapon-completion-close" aria-label="关闭完成报告" onClick={onClose}>×</button>
        </header>
        <main className="weapon-completion-layout">
          <section className="weapon-completion-stage" aria-label={successful ? '武器熔铸与冲压过程' : '任务状态'}>
            {successful ? (
              <>
                <div className="weapon-completion-vessel" aria-hidden="true">
                  <div className="weapon-completion-pool" />
                </div>
                <div className="forge-chamber" aria-hidden="true"><span>01 / MOLTEN FORMING</span><b>F</b><i/><em>CAST · PRESS · RELEASE</em></div>
                <div className="weapon-completion-stream" aria-hidden="true"><i /><b /></div>
                <div className="weapon-completion-ruler" aria-hidden="true">
                  <span className="weapon-completion-ruler-label">THERMAL / COOLING</span>
                  <span className="weapon-completion-ruler-value">1600°C</span>
                  <span className="weapon-completion-ruler-tick" />
                  <span className="weapon-completion-ruler-value weapon-completion-ruler-value--mid">950°C</span>
                  <span className="weapon-completion-ruler-tick weapon-completion-ruler-tick--mid" />
                  <span className="weapon-completion-ruler-value weapon-completion-ruler-value--low">300°C</span>
                  <span className="weapon-completion-ruler-tick weapon-completion-ruler-tick--low" />
                  <div className="weapon-completion-ruler-live" />
                </div>
                <div className="weapon-completion-die" aria-hidden="true">
                  <div className="weapon-completion-clamp weapon-completion-clamp--top" />
                  <div className="weapon-completion-clamp weapon-completion-clamp--bottom" />
                  <div className="weapon-completion-die-inset">
                    <svg className="weapon-completion-die-mark" viewBox="0 0 320 260" role="presentation">
                      <defs>
                        <linearGradient id={`${dieMaskId}-face`} x1="0" x2="1" y1="0" y2="1">
                          <stop offset="0" stopColor="#536f7b" />
                          <stop offset="0.48" stopColor="#263d48" />
                          <stop offset="1" stopColor="#3b5662" />
                        </linearGradient>
                        <linearGradient id={`${dieMaskId}-heat`} x1="0" x2="0" y1="0" y2="1">
                          <stop offset="0" stopColor="#ffc47a" />
                          <stop offset="0.5" stopColor="#ff772e" />
                          <stop offset="1" stopColor="#b9381c" />
                        </linearGradient>
                        <mask id={dieMaskId} maskUnits="userSpaceOnUse" x="0" y="0" width="320" height="260">
                          <rect width="320" height="260" fill="white" />
                          {Array.from({ length: rawRarity }, (_, index) => {
                            const centerX = 160 + (index - (rawRarity - 1) / 2) * 17
                            return <polygon key={index} fill="black" points={`${centerX},31 ${centerX + 4},35 ${centerX},39 ${centerX - 4},35`} />
                          })}
                          <text x="160" y="106" fill="black" textAnchor="middle" textLength="250" lengthAdjust="spacingAndGlyphs" fontFamily="Bahnschrift, Segoe UI, sans-serif" fontSize="52" fontWeight="700" letterSpacing="3">FORGED</text>
                          <text x="160" y="190" fill="black" textAnchor="middle" textLength="222" lengthAdjust="spacingAndGlyphs" fontFamily="Bahnschrift, Segoe UI, sans-serif" fontSize="52" fontWeight="700" letterSpacing="3">BLEND</text>
                        </mask>
                      </defs>
                      <rect x="3" y="3" width="314" height="254" fill={`url(#${dieMaskId}-heat)`} />
                      <rect className="weapon-completion-die-face" x="3" y="3" width="314" height="254" fill={`url(#${dieMaskId}-face)`} mask={`url(#${dieMaskId})`} />
                      <path d="M13 22V13h294v9M13 238v9h294v-9" fill="none" stroke="#c1d0d322" strokeWidth="1" />
                      <text x="160" y="148" fill="#ffad5c" textAnchor="middle" fontFamily="Consolas, monospace" fontSize="19" letterSpacing="7">//</text>
                    </svg>
                  </div>
                </div>
                <div className="weapon-completion-stage-state" aria-hidden="true"><i />{report.state === 'partial' ? 'PARTIAL FORGE' : 'FORGE COMPLETE'}</div>
              </>
            ) : (
              <div className="weapon-completion-plain-state">
                <span className="weapon-completion-plain-kicker">TASK / {stateLabel(report.state)}</span>
                <strong>{report.state === 'failed' ? '未完成' : '已取消'}</strong>
                <span>{report.name}</span>
                <code>{report.itemId}</code>
              </div>
            )}
          </section>
          <section className="weapon-completion-report" aria-label="武器资产报告">
            {successful && <div className="forge-report-art" aria-hidden="true">
              <b>FORGE</b><span>TOOLING / SECTION A—A</span>
              <svg viewBox="0 0 700 560" preserveAspectRatio="xMaxYMid slice">
                <g fill="none" stroke="currentColor">
                  <path d="M430-30V87L526 183V310L400 436V600M466-30V72L562 168V326L436 452V600M502-30V57L598 153V342L472 468V600"/>
                  <path d="M320 90H670M350 120H670M338 420H680M366 450H680M630 30V530" strokeDasharray="3 7"/>
                  <path d="M480 190H600V312H480Z M495 205H585V297H495Z M490 250H590"/>
                </g>
              </svg><small>FORM / STATIC ASSET<br/>MATERIAL — STRUCTURE — OUTPUT</small>
            </div>}
            <div className="weapon-completion-kicker"><span>{report.name}</span><span>{report.development ? 'DEV PREVIEW' : 'ITEM RECORD'}</span></div>
            <div className="weapon-completion-impression">
              {successful ? (
                <div className="weapon-completion-hammer">
                  <h1 id="weapon-completion-title" className="weapon-completion-title">{title}</h1>
                </div>
              ) : (
                <div className="weapon-completion-plain-title">
                  <span>{report.state === 'failed' ? 'PROCESS / FAILED' : 'PROCESS / CANCELLED'}</span>
                  <h1 id="weapon-completion-title">{title}</h1>
                </div>
              )}
              {successful && <svg className="weapon-completion-sparks" viewBox="-64 -64 128 128" aria-hidden="true">{sparkLines}</svg>}
            </div>
            <div className="weapon-completion-data-scroll" tabIndex={0} aria-label="产物与待处理项">
              <div className="weapon-completion-data-row" data-print-row><span>WEAPON_ID</span><strong>{report.weaponId ?? '未返回武器 ID'}</strong></div>
              <div className="weapon-completion-data-row" data-print-row><span>ITEM_ID</span><strong>{report.itemId}</strong></div>
              <div className="weapon-completion-data-row" data-print-row><span>VARIANT</span><strong>{report.variant === 'base' ? 'BASE' : 'REFINED_CANDIDATE'}</strong></div>
              <div className="weapon-completion-data-row" data-print-row><span>SEMANTICS</span><strong>{report.variantSemantics === 'source_base' ? 'SOURCE BASE' : report.variantSemantics === 'unverified_candidate' ? 'UNVERIFIED CANDIDATE' : '未返回'}</strong></div>
              <div className="weapon-completion-data-row" data-print-row><span>SOURCE</span><strong>{report.sourceResource ?? '未返回源路径'}</strong></div>
              <div className="weapon-completion-data-row" data-print-row><span>OUTPUT DIR</span><strong>{report.outputDirectory ?? (report.development ? 'DEV PREVIEW / no files created' : '未返回输出目录')}</strong></div>
              <div className="weapon-completion-data-row" data-print-row><span>STATUS</span><strong>{stateLabel(report.state)}</strong></div>
              <div className="weapon-completion-meta-strip" data-print-row>
                <div><span>OUTPUT FORMAT</span><strong>{report.artifacts.some(artifact => artifact.kind === 'blend') ? '.blend / standalone asset' : report.development ? 'DEV PREVIEW ONLY' : 'NO BLEND ARTIFACT'}</strong></div>
                <div><span>RARITY (RAW)</span><strong>{report.rarity ?? '—'}</strong></div>
                <div><span>REOPEN AUDIT</span><strong>{reopenAuditStatusFor(report)}</strong></div>
              </div>
              <div className="weapon-completion-data-group">
                <div className="weapon-completion-group-title" data-print-row>OUTPUT ARTIFACT / {report.artifacts.filter(artifact => artifact.kind === 'blend').length} · RECEIPTS / {report.artifacts.length - report.artifacts.filter(artifact => artifact.kind === 'blend').length}</div>
                {report.artifacts.length > 0 ? report.artifacts.map((artifact, index) => (
                  <div className="weapon-completion-file" data-print-row key={`${artifact.kind}-${index}-${artifact.path}`}><span className="weapon-completion-artifact-kind">{artifactLabels[artifact.kind]}</span><code>{artifact.path}</code></div>
                )) : <div className="weapon-completion-file" data-print-row><span>未返回产物文件</span></div>}
              </div>
              <div className="weapon-completion-data-group weapon-completion-data-group--pending">
                <div className="weapon-completion-group-title" data-print-row>PENDING / {report.pending.length}</div>
                {report.pending.length > 0 ? report.pending.map((item, index) => (
                  <div className="weapon-completion-pending" data-print-row key={`${index}-${item}`}><span>{item}</span></div>
                )) : <div className="weapon-completion-pending" data-print-row><span>无待处理项</span></div>}
              </div>
              {report.limitations.length > 0 && <div className="weapon-completion-data-group">
                <div className="weapon-completion-group-title" data-print-row>LIMITATIONS / {report.limitations.length}</div>
                {report.limitations.map((item, index) => <div className="weapon-completion-file" data-print-row key={`${index}-${item}`}><span>{item}</span></div>)}
              </div>}
              {report.error && <div className="weapon-completion-data-group weapon-completion-data-group--pending">
                <div className="weapon-completion-group-title" data-print-row>ERROR</div>
                <div className="weapon-completion-pending" data-print-row><span>{report.error}</span></div>
              </div>}
            </div>
            <footer className="weapon-completion-actions">
              <span>{successful ? 'REPORT / EXPORT REPORT' : 'REPORT / TASK RECORD'}</span>
              <div className="weapon-completion-actions-buttons">
                {successful && (
                  <button type="button" data-testid="weapon-completion-replay" onClick={() => setReplay(value => value + 1)}>
                    重播锻造 ↻
                  </button>
                )}
                {successful && <button type="button" data-testid="weapon-completion-slow" aria-pressed={slow} onClick={() => { setSlow(value => !value); setReplay(value => value + 1) }}>{slow ? '正常速度' : '慢速观察'}</button>}
                <button type="button" className="weapon-completion-return" data-testid="weapon-completion-return" onClick={onClose}>
                  返回武器档案 ↗
                </button>
              </div>
            </footer>
          </section>
        </main>
      </div>
    </dialog>
  )
}
