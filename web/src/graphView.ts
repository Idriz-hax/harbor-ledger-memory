export const MIN_ZOOM = 0.01
export const MAX_ZOOM = 50

export const viewLevelForZoom = (zoom: number, hasScope: boolean, currentLevel?: number) => {
  if (currentLevel === 0) return zoom > 0.48 ? 2 : 0
  if (currentLevel === 2) return zoom < 0.16 ? 0 : 2
  return zoom < 0.16 ? 0 : 2
}

const hash = (value: string) => {
  let acc = 0
  for (const char of value) acc = (acc * 31 + char.charCodeAt(0)) >>> 0
  return acc
}

/* Deterministic ring placement: the same (id, index, total) always yields the
   same coordinates, so bearings hold their bearing across refreshes. */
export const stablePosition = (id: string, index: number, total: number) => {
  const angle = (index / Math.max(1, total)) * Math.PI * 2
  const jitter = (hash(id) % 200) - 100
  const radius = 700 + jitter
  return { x: Math.round(Math.cos(angle) * radius * 10) / 10, y: Math.round(Math.sin(angle) * radius * 10) / 10 }
}

export type GraphCluster = {
  id: string
  label: string
  kind: string
  type_counts: Record<string, number>
  member_count: number
  scope: string
}

export type GraphEdge = {
  id: string
  source: string
  target: string
  weight: number
  edge_type: string
}

export type GraphView = {
  level: number
  scope: string | null
  clusters: GraphCluster[]
  edges: GraphEdge[]
  generation?: string
}
