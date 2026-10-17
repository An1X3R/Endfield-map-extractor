import { useEffect, useMemo, useRef, useState } from 'react'
import { navigateCatalog } from './catalogNavigation'

export type ActionCatalogOption = {
  value: string
  label: string
  group: 'action' | 'reference'
  disabled: boolean
  detail: string
}

type Props = {
  active: boolean
  disabled: boolean
  loading: boolean
  options: ActionCatalogOption[]
  value: string
  total: number
  hasMore: boolean
  onChange: (value: string) => void
  onLoadMore: () => void
}

const GROUPS = [{ value: 'action', label: '动作目录' }, { value: 'reference', label: '精确 NPC 动作引用' }] as const

export function ActionCatalog({ active, disabled, loading, options, value, total, hasMore, onChange, onLoadMore }: Props) {
  const dialog = useRef<HTMLDialogElement>(null)
  const content = useRef<HTMLDivElement>(null)
  const collapse = useRef<HTMLButtonElement>(null)
  const done = useRef<HTMLButtonElement>(null)
  const savedScroll = useRef(0)
  const [query, setQuery] = useState('')
  const [limit, setLimit] = useState(80)
  const matched = useMemo(() => {
    const term = query.trim().toLowerCase()
    return options.filter(option => `${option.label} ${option.value}`.toLowerCase().includes(term))
  }, [options, query])
  const selected = options.find(option => option.value === value)

  useEffect(() => {
    if (!active && dialog.current?.open) {
      savedScroll.current = content.current?.scrollTop ?? 0
      dialog.current.close()
    }
  }, [active])

  const open = (): void => {
    dialog.current?.showModal()
    if (content.current) content.current.scrollTop = savedScroll.current
  }
  const close = (): void => {
    savedScroll.current = content.current?.scrollTop ?? 0
    dialog.current?.close()
  }

  return <>
    <button type="button" className="catalog-expand-toggle" data-testid="operator-action-expand" disabled={disabled || !options.length} aria-haspopup="dialog" aria-controls="operator-action-catalog" onClick={open}>展开动作目录 ↗<small>{options.length} LOADED</small></button>
    <dialog ref={dialog} id="operator-action-catalog" data-testid="operator-action-catalog" className="action-catalog" aria-labelledby="action-catalog-title" onCancel={event => { event.preventDefault(); close() }} onKeyDown={event => {
      if (event.key === 'Escape') { event.preventDefault(); event.stopPropagation(); close(); return }
      if (event.key !== 'Tab') return
      if (event.shiftKey && event.target === collapse.current) { event.preventDefault(); done.current?.focus() }
      else if (!event.shiftKey && event.target === done.current) { event.preventDefault(); collapse.current?.focus() }
    }}>
      <header className="action-catalog-header"><div><small>SINGLE ACTION / INDEX</small><h2 id="action-catalog-title">动作目录</h2></div><button ref={collapse} type="button" className="catalog-expand-toggle" data-testid="operator-action-collapse" onClick={close}>收起 ↙ <small>ESC</small></button></header>
      <label className="search-field"><span aria-hidden="true">⌕</span><input type="search" data-testid="operator-action-search" aria-label="检索已载入的动作名称或代号" placeholder="检索已载入的动作名称 / 代号" value={query} onChange={event => { setQuery(event.currentTarget.value); setLimit(80); savedScroll.current = 0; if (content.current) content.current.scrollTop = 0 }} /></label>
      <p className="action-catalog-scope">单选动作 · 已载入 {options.length} 项{total > options.length ? ` / 目录 ${total} 项` : ''} · 命中 {matched.length} 项 · 目录发现不代表绑定已验收{hasMore ? ' · 搜索仅覆盖已载入内容，可继续读取目录' : ''}</p>
      <div ref={content} className="action-catalog-content" onKeyDown={navigateCatalog}>
        {GROUPS.map(group => {
          const rows = matched.slice(0, limit).filter(option => option.group === group.value)
          return rows.length > 0 && <section key={group.value}><h3>{group.label}</h3><div className="action-catalog-grid">{rows.map(option => <button type="button" id={`operator-action-option-${encodeURIComponent(option.value)}`} data-testid="operator-action-option" data-catalog-item="" data-action-value={option.value} key={option.value} aria-pressed={value === option.value} disabled={disabled || option.disabled} title={`${option.label}\n${option.value}\n${option.detail}`} onClick={() => onChange(option.value)}><b>{option.label}</b>{option.disabled && <span>不可提交 · {option.detail}</span>}</button>)}</div></section>
        })}
        {!matched.length && <p className="no-results">已载入的目录中没有匹配动作。</p>}
        {matched.length > limit && <button type="button" className="operator-load-more" data-testid="operator-action-more-visible" onClick={() => setLimit(current => current + 80)}>继续显示 · {matched.length - limit} 项</button>}
        {hasMore && <button type="button" className="operator-load-more" data-testid="operator-action-load-more" disabled={loading} onClick={onLoadMore}>{loading ? '正在读取…' : '继续读取动作目录'} · 已载入 {options.length} 项</button>}
      </div>
      <footer className="action-catalog-footer"><div><small>当前绑定 / SELECTED</small><strong title={selected ? `${selected.value} · ${selected.detail}` : undefined} data-testid="operator-action-current">{selected?.label ?? '尚未选择动作'}</strong></div><button ref={done} type="button" className="secondary-button" data-testid="operator-action-done" onClick={close}>返回提取设置 →</button></footer>
    </dialog>
  </>
}
