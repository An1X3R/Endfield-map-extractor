import { useCallback, useEffect, useState } from 'react'
import { getAssetCatalog } from './api'
import type { AssetCatalogByKind, AssetKind } from './types'

export type AssetCatalogController<K extends AssetKind> = {
  catalog: AssetCatalogByKind[K] | null; loading: boolean; error: string | null; reload: () => void
}
export const useAssetCatalog = <K extends AssetKind>(kind: K, active: boolean): AssetCatalogController<K> => {
  const [catalog, setCatalog] = useState<AssetCatalogByKind[K] | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [revision, setRevision] = useState(0)
  const reload = useCallback((): void => setRevision(value => value + 1), [])
  useEffect(() => {
    if (!active) return
    const controller = new AbortController()
    setLoading(true)
    setError(null)
    void getAssetCatalog(kind, controller.signal).then(result => {
      if (!controller.signal.aborted) setCatalog(result)
    }).catch((error: Error) => {
      if (!controller.signal.aborted) setError(error.message)
    }).finally(() => {
      if (!controller.signal.aborted) setLoading(false)
    })
    return () => controller.abort()
  }, [kind, active, revision])
  return { catalog, loading, error, reload }
}
