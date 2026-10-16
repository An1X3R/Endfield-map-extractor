import { useEffect, useState } from 'react'
import inkbiter from './assets/inkbiter.svg'

// Original SVG based on the supplied front view of Si's showcase companion, not the enemy model.
// Source manifest: webui_creature_images/inkbiter, source SHA256 203e71a2be20e794e147ded9fbf5bd9cc4d919f7ada6e3e14cb2d4caad62fed2.
export function InkSpirit({ active }: { active: boolean }) {
  const [visible, setVisible] = useState(false)
  useEffect(() => { if (!active) setVisible(false) }, [active])
  return <div className={`si-ink-spirit-slot ${visible && active ? 'is-visible' : ''}`} data-testid="si-ink-spirit-slot" hidden={!active}>
    <img src={inkbiter} alt="" aria-hidden="true" width="160" height="160" />
    <button type="button" data-testid="si-ink-spirit-trigger" aria-label="唤出墨魉" aria-expanded={visible} onPointerEnter={() => setVisible(true)} onClick={() => setVisible(true)} onBlur={() => setVisible(false)} onPointerLeave={() => setVisible(false)}>墨 · 魉</button>
  </div>
}
