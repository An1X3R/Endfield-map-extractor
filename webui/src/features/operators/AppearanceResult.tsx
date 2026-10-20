import { describeAppearance } from './appearance'
import type { CharacterAppearanceReport } from './types'

export function AppearanceResult({ report }: { report: CharacterAppearanceReport }) {
  const verification = report.fur.verification
  const evidence = [...report.potential.pending ?? [], ...report.fur.pending ?? []]
  return <div className="appearance-result" data-testid="operator-appearance-result">
    <p><b>实际外观 · {report.status === 'partial' ? '有限支持' : '处理完成'}</b><br />{describeAppearance(report)}</p>
    {verification && <p className="help">{verification.source_shell_count !== undefined && <>源绒面 {verification.source_shell_count} 项 · </>}{verification.artist_applied_count !== undefined && <>炭绒应用 {verification.artist_applied_count} 项 · </>}{verification.artist_skipped_count !== undefined && <>不匹配跳过 {verification.artist_skipped_count} 项 · </>}{verification.skipped_hidden_source_count !== undefined && <>隐藏源跳过 {verification.skipped_hidden_source_count} 项</>}</p>}
    <p className="help">满潜粒子为固定展示快照，并非完整原生动态。炭绒为兼容源材质上的近似表现。</p>
    {(evidence.length > 0 || report.potential.scope) && <details data-testid="operator-appearance-evidence"><summary>外观处理依据与限制{evidence.length > 0 ? ` · ${evidence.length} 项待处理记录` : ''}</summary>{report.potential.scope && <p>{report.potential.scope}</p>}{evidence.map((item, index) => <pre key={index}>{item}</pre>)}</details>}
  </div>
}
