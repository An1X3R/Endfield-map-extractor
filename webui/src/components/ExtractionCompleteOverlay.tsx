import { useEffect } from 'react'
import type { CSSProperties } from 'react'
import { AnimatePresence, motion, useReducedMotion } from 'motion/react'
import type { BlenderBuildResult, ExportMode, MapId } from '../types'

type Props = {
  visible: boolean
  mapId: MapId
  mapName: string
  sectorCount: number
  exportMode: ExportMode
  blenderBuild: BlenderBuildResult | null
  onFinished: () => void
}

const completionCopy: Record<MapId, { code: string; detail: string }> = {
  map01: {
    code: 'VALLEY IV / EXTRACTION',
    detail: 'INDUSTRIAL REGION PACKAGE READY',
  },
  map02: {
    code: 'WULING / EXTRACTION',
    detail: 'XIRANG REGION PACKAGE READY',
  },
}

export function ExtractionCompleteOverlay({
  visible,
  mapId,
  mapName,
  sectorCount,
  exportMode,
  blenderBuild,
  onFinished,
}: Props) {
  const reduceMotion = useReducedMotion()
  const copy = completionCopy[mapId]
  const scenes = blenderBuild?.scenes ?? []
  const partialSceneBuild = exportMode === 'blend' && (
    blenderBuild?.status === 'partial' || (blenderBuild?.pending ?? 0) > 0
  )

  useEffect(() => {
    if (!visible || exportMode === 'blend') return
    const timer = window.setTimeout(onFinished, reduceMotion ? 1700 : 4100)
    return () => window.clearTimeout(timer)
  }, [exportMode, onFinished, reduceMotion, visible])

  return (
    <AnimatePresence initial={false}>
      {visible && (
        <motion.section
          className={`extraction-complete extraction-complete--${mapId}`}
          role="status"
          aria-live="assertive"
          aria-label={`${mapName}提取完成`}
          initial={reduceMotion ? { opacity: 0 } : { y: '100%' }}
          animate={reduceMotion ? { opacity: 1 } : { y: 0 }}
          exit={reduceMotion ? { opacity: 0 } : { y: '-104%' }}
          transition={{
            duration: reduceMotion ? 0.12 : 0.82,
            ease: [0.16, 1, 0.3, 1],
          }}
        >
          <div className="completion-field" aria-hidden="true">
            <div className="completion-field__grid" />
            <div className="completion-field__scan" />
            <div className="completion-field__blocks">
              {Array.from({ length: 18 }, (_, index) => (
                <i key={index} style={{ '--complete-index': index } as CSSProperties} />
              ))}
            </div>
            <div className="completion-field__ribbons">
              {Array.from({ length: 5 }, (_, index) => (
                <i key={index} style={{ '--ribbon-index': index } as CSSProperties} />
              ))}
            </div>
          </div>

          <div className="completion-frame" aria-hidden="true">
            <i className="completion-frame__top" />
            <i className="completion-frame__right" />
            <i className="completion-frame__bottom" />
            <i className="completion-frame__left" />
          </div>

          <motion.div
            className="completion-lockup"
            initial={reduceMotion ? false : { opacity: 0, y: 24, scale: 0.96 }}
            animate={{ opacity: 1, y: 0, scale: 1 }}
            transition={{
              duration: reduceMotion ? 0.1 : 0.56,
              delay: reduceMotion ? 0 : 0.48,
              ease: [0.16, 1, 0.3, 1],
            }}
          >
            <div className="completion-symbol" aria-hidden="true">
              <svg viewBox="0 0 160 160">
                <motion.g
                  className="completion-symbol__ring-group"
                  initial={reduceMotion ? false : { rotate: -150 }}
                  animate={{ rotate: reduceMotion ? 0 : 210 }}
                  transition={{
                    duration: reduceMotion ? 0.1 : 1.14,
                    delay: reduceMotion ? 0 : 0.48,
                    ease: [0.2, 0.72, 0.18, 1],
                  }}
                >
                  <circle className="completion-symbol__ghost" cx="80" cy="80" r="58" />
                  <motion.circle
                    className="completion-symbol__ring"
                    cx="80"
                    cy="80"
                    r="58"
                    pathLength={1}
                    initial={reduceMotion ? { pathLength: 1 } : { pathLength: 0.03 }}
                    animate={{ pathLength: 1 }}
                    transition={{
                      duration: reduceMotion ? 0.1 : 1.04,
                      delay: reduceMotion ? 0 : 0.5,
                      ease: [0.33, 0, 0.2, 1],
                    }}
                  />
                </motion.g>
                <motion.path
                  className="completion-symbol__check"
                  d="M43 84 L69 106 L140 34 M118 35 L140 34 L138 56"
                  pathLength={1}
                  initial={reduceMotion ? { pathLength: 1 } : { pathLength: 0 }}
                  animate={{ pathLength: 1 }}
                  transition={{
                    duration: reduceMotion ? 0.1 : 0.62,
                    delay: reduceMotion ? 0 : 1.38,
                    ease: [0.16, 1, 0.3, 1],
                  }}
                />
              </svg>
              <motion.i
                className="completion-symbol__orbit"
                initial={reduceMotion ? false : { rotate: 0, opacity: 0 }}
                animate={{ rotate: reduceMotion ? 0 : 360, opacity: 1 }}
                transition={{
                  rotate: {
                    duration: reduceMotion ? 0.1 : 1.3,
                    delay: reduceMotion ? 0 : 0.44,
                    ease: [0.16, 1, 0.3, 1],
                  },
                  opacity: { duration: 0.18, delay: reduceMotion ? 0 : 0.44 },
                }}
              />
            </div>

            <motion.div
              className={`completion-copy ${partialSceneBuild ? 'completion-copy--partial' : ''}`}
              initial={reduceMotion ? false : { opacity: 0, x: -18 }}
              animate={{ opacity: 1, x: 0 }}
              transition={{
                duration: reduceMotion ? 0.1 : 0.5,
                delay: reduceMotion ? 0 : 1.58,
                ease: [0.16, 1, 0.3, 1],
              }}
            >
              <span>{copy.code}</span>
              <h2>{exportMode === 'blend'
                ? partialSceneBuild ? '场景已导出 · 有待处理项' : '场景已导出'
                : '提取完成'}</h2>
              <p>{mapName} / {sectorCount} SECTORS</p>
              <small>{exportMode === 'blend'
                ? partialSceneBuild ? 'BLEND FILES VERIFIED / ITEMS REMAIN PENDING' : 'BLEND FILES VERIFIED / SCENE EXPORT COMPLETE'
                : copy.detail}</small>
            </motion.div>
          </motion.div>

          {exportMode === 'blend' && (
            <div className="completion-scene-report" aria-label="Blender 场景构建结果">
              <div className="completion-scene-report__heading">
                <div>
                  <strong>{partialSceneBuild ? 'BLENDER SCENES / PARTIAL' : 'BLENDER SCENES / VERIFIED'}</strong>
                  <span>{scenes.length} 批 · 路径相对输出目录 · {blenderBuild?.pending ?? 0} 项待处理</span>
                </div>
                <button type="button" onClick={onFinished}>关闭</button>
              </div>
              {scenes.length > 0 ? (
                <>
                  <ul>
                    {scenes.map((scene) => (
                      <li key={scene.path}>
                        <code title={scene.path}>{scene.path}</code>
                        <span>{scene.instances} 个实例 · {scene.pending} 项待处理</span>
                      </li>
                    ))}
                  </ul>
                  {partialSceneBuild && <p>待处理数包含尚未接入的组件，不等同于缺失资产数量；场景文件已写入并通过重开校验。</p>}
                </>
              ) : (
                <p>任务已完成，但后端没有返回场景路径；请检查导出报告。</p>
              )}
            </div>
          )}

          <motion.div
            className="completion-confirm-line"
            aria-hidden="true"
            initial={reduceMotion ? false : { scaleX: 0 }}
            animate={{ scaleX: 1 }}
            transition={{
              duration: reduceMotion ? 0.1 : 0.92,
              delay: reduceMotion ? 0 : 1.72,
              ease: [0.16, 1, 0.3, 1],
            }}
          />
        </motion.section>
      )}
    </AnimatePresence>
  )
}
