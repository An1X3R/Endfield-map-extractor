import { MAPS } from '../data/maps'
import type { MapId } from '../types'
import './RegionCartouche.css'

type RegionCartoucheProps = {
  mapId: MapId
}

export function RegionCartouche({ mapId }: RegionCartoucheProps) {
  const map = MAPS[mapId]
  const dimension = map.worldBounds.xmax - map.worldBounds.xmin
  const index = mapId === 'map01' ? '01' : '02'

  return (
    <aside className="region-cartouche" aria-label="地区信息" data-region={mapId}>
      <div className="cartouche-index">
        <span>{index}</span>
        <span>REGION<br />ATLAS</span>
      </div>
      <div className="cartouche-body">
        <div>
          <span className="cartouche-rule" />
          <h2>{map.chineseName}</h2>
          <p>区域全景 / 北向上</p>
        </div>
        <svg className="region-sculpture" viewBox="0 0 120 132" aria-hidden="true">
          <defs>
            <linearGradient id="mineral" x1="0" y1="0" x2="1" y2="1">
              <stop stopColor="#b1c3b8" />
              <stop offset="1" stopColor="#617e70" />
            </linearGradient>
          </defs>
          {mapId === 'map02' && (
            <g>
              <path d="m14 17 42-14 48 16-43 15Z" fill="#c9d2c7" />
              <path d="M14 17 61 34v45L42 72Q49 40 14 47Z" fill="url(#mineral)" />
              <path d="m61 34 43-15v30Q74 39 79 74l-18 5Z" fill="#486357" />
              <path d="m14 76 19-7q6 31 28 20 20 10 25-20l18 8-43 18Z" fill="#b6c6bc" />
              <path d="m14 76 47 19v34l-47-17Z" fill="#7a9486" />
              <path d="m61 95 43-18v36l-43 16Z" fill="#3b5549" />
              <path d="M15 91q31 3 29 31m35-35q-7 18 24 9" fill="none" stroke="#b9cfc0" strokeWidth="1" opacity=".4" />
            </g>
          )}
          {mapId === 'map01' && (
            <g fill="none" stroke="currentColor" strokeWidth="5">
              <path d="m12 110 95-75M12 80l95-75M30 120V20m30 100V20m30 100V20" />
            </g>
          )}
        </svg>
      </div>
      <div className="cartouche-meta">
        <span>{dimension} × {dimension}</span>
        <span>正交视图</span>
      </div>
    </aside>
  )
}
