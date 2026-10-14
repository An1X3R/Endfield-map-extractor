import { useEffect, useRef, useState } from 'react'
import type { SyntheticEvent } from 'react'
import { useReducedMotion } from 'motion/react'
import type {
  InstancedSceneExportGroup,
  InstancedSceneExportMode,
  InstancedSceneJob,
} from './types'

type ResultStatus = 'success' | 'partial' | 'failed' | 'cancelled'
type DevelopmentStatus = ResultStatus

export type InstancedSceneResultView =
  | {
      kind: 'development'
      sceneId: string
      sceneName: string
      exportMode: InstancedSceneExportMode
      exportGroups: InstancedSceneExportGroup[]
    }
  | {
      kind: 'job'
      job: InstancedSceneJob
      sceneName: string
    }

type Props = {
  result: InstancedSceneResultView
  onClose: () => void
}

const EXPORT_GROUPS: Array<[InstancedSceneExportGroup, string]> = [
  ['building', '建筑'],
  ['prop', '道具'],
  ['lighting', '光源'],
  ['particle', '粒子'],
  ['vegetation', '植被'],
  ['terrain', '地形'],
  ['road', '道路'],
  ['water', '水体'],
  ['effects', '特效'],
  ['unknown', '未知 / 其它'],
]

const RESULT_COPY: Record<ResultStatus, { label: string; title: string; message: string; english: string }> = {
  success: {
    label: '完成',
    title: '导出完成',
    message: '任务已结束。',
    english: '// EXTRACTION COMPLETED',
  },
  partial: {
    label: '待处理',
    title: '导出完成，有待处理项',
    message: '部分构件仍需检查。',
    english: '// PARTIAL RESULT',
  },
  failed: {
    label: '失败',
    title: '导出失败',
    message: '任务未完成。',
    english: '// EXTRACTION FAILED',
  },
  cancelled: {
    label: '取消',
    title: '任务已取消',
    message: '操作已取消。',
    english: '// TASK CANCELLED',
  },
}

const exportModeLabel = (mode: InstancedSceneExportMode): string =>
  mode === 'blend' ? 'Blender 场景（含审计报告）' : '审计数据包'

const resultStatusFromJob = (job: InstancedSceneJob): ResultStatus => {
  if (job.state === 'completed') return 'success'
  if (job.state === 'partial') return 'partial'
  if (job.state === 'failed') return 'failed'
  if (job.state === 'cancelled') return 'cancelled'
  throw new Error(`Cannot show a result dialog for non-terminal scene job ${job.job_id} in state ${job.state}.`)
}

const formatBytes = (bytes: number): string => {
  if (!Number.isFinite(bytes) || bytes < 0) throw new RangeError(`Invalid scene result file size: ${bytes}`)
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 ** 2) return `${(bytes / 1024).toFixed(1)} KB`
  if (bytes < 1024 ** 3) return `${(bytes / 1024 ** 2).toFixed(1)} MB`
  return `${(bytes / 1024 ** 3).toFixed(1)} GB`
}

export function InstancedSceneResult({ result, onClose }: Props) {
  const reducedMotion = useReducedMotion() === true
  const dialogRef = useRef<HTMLDialogElement | null>(null)
  const closeRef = useRef<HTMLButtonElement | null>(null)
  const [developmentStatus, setDevelopmentStatus] = useState<DevelopmentStatus>('success')
  const [replay, setReplay] = useState(0)
  const isDevelopment = result.kind === 'development'
  const job = result.kind === 'job' ? result.job : null
  const status = isDevelopment ? developmentStatus : resultStatusFromJob(result.job)
  const copy = RESULT_COPY[status]
  const title = copy.title
  const message = isDevelopment ? copy.message : job?.message || copy.message
  const outputDirectory = job?.result?.output_directory ?? null
  const outputDisplay = outputDirectory ?? (isDevelopment ? '开发样例：未生成文件' : '任务未返回输出目录')
  const files = job?.result?.files ?? []
  const pendingItems = job?.result?.pending ?? []

  useEffect(() => {
    const dialog = dialogRef.current
    if (!dialog) throw new Error('Instanced scene result dialog is missing.')
    const previousFocus = document.activeElement instanceof HTMLElement ? document.activeElement : null
    dialog.showModal()
    closeRef.current?.focus()
    return () => {
      if (dialog.open) dialog.close()
      if (previousFocus?.isConnected) previousFocus.focus()
    }
  }, [])

  const handleCancel = (event: SyntheticEvent<HTMLDialogElement>): void => {
    event.preventDefault()
    onClose()
  }

  return (
    <dialog
      ref={dialogRef}
      className="isc-result-dialog"
      data-state={status}
      aria-labelledby="isc-result-title"
      aria-describedby="isc-result-message"
      data-testid="instanced-scene-result-dialog"
      onCancel={handleCancel}
    >
      <div className="isc-result-top">
        <span>INSTANCED SCENES / EXPORT RESULT</span>
        <button ref={closeRef} data-testid="instanced-scene-result-close" type="button" aria-label="关闭结果" onClick={onClose}>×</button>
      </div>
      <div className="isc-result-content">
        <div key={replay} className={`isc-result-motion${reducedMotion ? '' : ' is-playing'}`} aria-hidden="true">
          <div className="isc-emblem">
            <div className="isc-emblem-dither" />
            <span className="isc-emblem-copy">{copy.title}<small>{copy.english}</small></span>
          </div>
          <svg className="isc-check" viewBox="0 0 240 240" aria-hidden="true">
            <path className="isc-ring" d="M215 120 A95 95 0 1 0 25 120 A95 95 0 1 0 215 120" />
            <path className="isc-cut" d="M187.175 52.825 A95 95 0 0 1 197.819 65.51" />
            <path className="isc-tick" d="M59 121 L101 162 L230 32" pathLength="1" />
          </svg>
          <div className="isc-particles"><i /><i /><i /><i /><i /></div>
          <div className="isc-seal-record">
            <small>结果记录 / SCENE RECORD</small>
            <strong>{result.kind === 'job' ? result.sceneName : `${result.sceneName} / ${result.sceneId}`}</strong>
            <span>{outputDisplay}</span>
            <span>{isDevelopment ? '样例状态，不含真实任务或构件统计' : `${files.length} 个结果文件 · ${pendingItems.length} 项待处理`}</span>
          </div>
        </div>
        <div className="isc-result-copy">
          <small>{isDevelopment ? `DEV / 状态样例 / ${copy.label}` : `EXPORT / ${copy.label}`}</small>
          <h2 id="isc-result-title">{title}</h2>
          <p id="isc-result-message">{status === 'partial' ? `${message} 待处理项数量不等于缺失资产数量。` : message}</p>
          <dl>
            <div><dt>场景</dt><dd>{result.kind === 'job' ? `${result.sceneName} / ${job?.request.scene_id}` : `${result.sceneName} / ${result.sceneId}`}</dd></div>
            <div><dt>输出格式</dt><dd>{result.kind === 'job' ? exportModeLabel(result.job.request.export_mode) : exportModeLabel(result.exportMode)}</dd></div>
            <div><dt>结果路径</dt><dd>{outputDisplay}</dd></div>
            <div><dt>处理信息</dt><dd>{isDevelopment ? '开发样例未提交任务' : `${job?.message ?? ''}${job?.error ? ` · ${job.error.code}` : ''}`}</dd></div>
          </dl>
          {isDevelopment ? (
            <p className="isc-result-dev-note">这是界面样例，未提交任务、未创建文件；实际状态以正式任务报告为准。</p>
          ) : (
            <>
              {job?.error && (
                <p className="isc-result-error">
                  {job.error.stage ? `${job.error.stage} · ` : ''}{job.error.code}: {job.error.message}
                  {job.error.retryable ? ' · 可重试' : ''}
                </p>
              )}
              {files.length > 0 && (
                <ul className="isc-result-files" aria-label="结果文件">
                  {files.map((file) => (
                    <li key={`${file.role}:${file.relative_path}`}>
                      <code>{file.relative_path}</code><span>{file.role} · {formatBytes(file.bytes)}</span>
                    </li>
                  ))}
                </ul>
              )}
              {job?.result && (
                <ul className="isc-result-pending" aria-label="构件统计与待处理项">
                  {EXPORT_GROUPS.map(([groupId, label]) => {
                    const counts = job.result?.component_summary[groupId]
                    if (!counts) throw new Error(`Scene result component summary is missing group ${groupId}.`)
                    const formatCount = (value: number | null): string => value === null ? '—' : value.toLocaleString()
                    return <li key={groupId}><span>{label}</span><span>导出 {formatCount(counts.exported)} · 待处理 {formatCount(counts.pending)} · 不支持 {formatCount(counts.unsupported)}</span></li>
                  })}
                  {pendingItems.map((item, index) => (
                    <li key={`${item.group_id}:${item.code}:${index}`}><strong>{item.group_id} / {item.code}</strong><span>{item.message}</span></li>
                  ))}
                </ul>
              )}
            </>
          )}
          <div className="isc-result-controls">
            {isDevelopment && (
              <label>预览状态
                <select
                  data-testid="instanced-scene-result-state"
                  value={developmentStatus}
                  onChange={(event) => {
                    const value = event.currentTarget.value
                    if (value !== 'success' && value !== 'partial' && value !== 'failed' && value !== 'cancelled') {
                      throw new Error(`Unsupported development result state: ${value}`)
                    }
                    setDevelopmentStatus(value)
                    setReplay((current) => current + 1)
                  }}
                >
                  <option value="success">导出完成</option>
                  <option value="partial">导出完成，有待处理项</option>
                  <option value="failed">导出失败</option>
                  <option value="cancelled">任务已取消</option>
                </select>
              </label>
            )}
            <button data-testid="instanced-scene-result-replay" type="button" onClick={() => setReplay((current) => current + 1)}>重播动画 ↻</button>
            <button data-testid="instanced-scene-result-return" type="button" onClick={onClose}>返回工作台 ↗</button>
          </div>
        </div>
      </div>
    </dialog>
  )
}
