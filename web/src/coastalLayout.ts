export type AtlasPoint = { x: number; y: number }
export type AtlasNode = { id: string; scope: string; kind: string }
export type AtlasEdge = { source: string; target: string }
export type Island = { key: string; center: AtlasPoint; radius: number; nodeIds: string[] }
export type AtlasPlacement = { positions: Record<string, AtlasPoint>; islands: Island[] }

const hash = (value: string) => [...value].reduce((acc, char) => ((acc * 31) + char.charCodeAt(0)) >>> 0, 2166136261)
const unit = (value: string) => (hash(value) % 10_000) / 10_000

const firstScopeSegment = (scope: string) => scope.includes('/') ? scope.split('/')[0] : '(root)'

const sunflowerAngle = Math.PI * (3 - Math.sqrt(5))

/** Index-ness is a path concern, not a backend kind label. */
export const isIndexPath = (scope: string) => /(^|\/)index\.md$/i.test(scope)

export const nodeRadius = (node: AtlasNode, linked: boolean) =>
  linked ? 70 + unit(`${node.id}:coast`) * 34 : 166 + unit(`${node.id}:water`) * 48

export const buildCoastalPlacement = (
  nodes: AtlasNode[],
  edges: AtlasEdge[],
  overrides: Record<string, AtlasPoint>,
  layoutSeed?: string | number,
): AtlasPlacement => {
  const groups = new Map<string, AtlasNode[]>()
  for (const node of nodes) {
    const key = firstScopeSegment(node.scope)
    const group = groups.get(key) ?? []
    group.push(node)
    groups.set(key, group)
  }

  const keys = [...groups.keys()].sort()
  const seedOffset = layoutSeed === undefined ? 0 : unit(`layout:${layoutSeed}`) * Math.PI * 2
  const degree = new Map<string, number>()
  for (const edge of edges) {
    degree.set(edge.source, (degree.get(edge.source) ?? 0) + 1)
    degree.set(edge.target, (degree.get(edge.target) ?? 0) + 1)
  }

  // Build each island in local space first. A sunflower starts with a node at
  // the island center, then expands outward, so groups stay compact without
  // leaving the visually important middle empty.
  const local = new Map<string, { x: number; y: number }>()
  const islandRadii = new Map<string, number>()
  keys.forEach(key => {
    const nodeIds = (groups.get(key) ?? []).map(node => node.id).sort()
    const fieldRadius = Math.max(...nodeIds.map(nodeId => {
      const node = groups.get(key)!.find(candidate => candidate.id === nodeId)!
      return nodeRadius(node, (degree.get(nodeId) ?? 0) > 0)
    }), 0)
    const radialStep = nodeIds.length < 2
      ? 0
      : Math.max(64, fieldRadius / Math.sqrt(nodeIds.length - 1))
    let maxRadius = 0
    nodeIds.forEach((nodeId, index) => {
      const radius = radialStep * Math.sqrt(index)
       const angle = index === 0 ? 0 : index * sunflowerAngle - Math.PI / 2 + seedOffset
      local.set(nodeId, { x: Math.cos(angle) * radius, y: Math.sin(angle) * radius })
      maxRadius = Math.max(maxRadius, radius)
    })
    nodeIds.forEach(nodeId => {
      const point = local.get(nodeId)!
      maxRadius = Math.max(maxRadius, Math.hypot(point.x, point.y))
    })
    islandRadii.set(key, maxRadius + 34)
  })

  // Incremental spiral disk packing keeps the first island at the origin and
  // puts every following island at the first compact, non-overlapping spot.
  const packedCenters: Array<{ center: AtlasPoint; radius: number }> = []
  const packCenter = (radius: number): AtlasPoint => {
    if (packedCenters.length === 0) return { x: 0, y: 0 }
    for (let distance = 8; distance < 6000; distance += 8) {
      const samples = Math.max(16, Math.ceil((Math.PI * 2 * distance) / 16))
      for (let sample = 0; sample < samples; sample += 1) {
         const angle = sample * sunflowerAngle + distance * 0.013 + seedOffset
        const candidate = { x: Math.round(Math.cos(angle) * distance), y: Math.round(Math.sin(angle) * distance) }
        if (packedCenters.every(previous =>
          Math.hypot(candidate.x - previous.center.x, candidate.y - previous.center.y)
            >= radius + previous.radius + 2
        )) return candidate
      }
    }
    return { x: packedCenters.length * (radius + 2), y: 0 }
  }

  const islands = keys.map(key => {
    const radius = islandRadii.get(key)!
    const center = packCenter(radius)
    packedCenters.push({ center, radius })
    const nodeIds = (groups.get(key) ?? []).map(node => node.id).sort()
    return { key, center, radius, nodeIds }
  })

  const positions: Record<string, AtlasPoint> = {}
  for (const island of islands) {
    island.nodeIds.forEach(nodeId => {
      const override = overrides[nodeId]
      if (override) {
        positions[nodeId] = override
        return
      }

       const point = local.get(nodeId)!
       positions[nodeId] = {
         x: Math.round(island.center.x + point.x),
         y: Math.round(island.center.y + point.y),
       }
    })
  }

  return { positions, islands }
}

/* Bump this namespace when the generated coastline changes. Old manual
 * positions are intentionally left in storage, but must not override a new
 * layout revision. Future drags continue to persist under the current key. */
export const POSITION_LAYOUT_VERSION = 'layout-v2'
const storageKey = (generation: string) => `hlm:tide-atlas:positions:${POSITION_LAYOUT_VERSION}:${generation}`

export const loadPositionOverrides = (generation: string): Record<string, AtlasPoint> =>
  JSON.parse(localStorage.getItem(storageKey(generation)) ?? '{}')

export const savePositionOverride = (generation: string, nodeId: string, point: AtlasPoint): void => {
  const current = loadPositionOverrides(generation)
  localStorage.setItem(storageKey(generation), JSON.stringify({ ...current, [nodeId]: point }))
}

export const clearPositionOverrides = (generation: string): void => {
  localStorage.removeItem(storageKey(generation))
}

/**
 * A deliberately small settle pass for a freshly released node. It only
 * touches the dragged node and its immediate neighbours; ordinary pan/zoom
 * never calls this function and therefore never causes a layout or request.
 */
export const settleOneHop = (
  positions: Record<string, AtlasPoint>,
  edges: AtlasEdge[],
  movedId: string,
  bounds = 72,
): Record<string, AtlasPoint> => {
  const next = { ...positions }
  const neighbours = new Set<string>()
  edges.forEach(edge => {
    if (edge.source === movedId) neighbours.add(edge.target)
    if (edge.target === movedId) neighbours.add(edge.source)
  })
  const moved = next[movedId]
  if (!moved) return next
  for (const id of neighbours) {
    const point = next[id]
    if (!point) continue
    const dx = point.x - moved.x
    const dy = point.y - moved.y
    const distance = Math.hypot(dx, dy) || 1
    const minDistance = 42
    if (distance >= minDistance) continue
    const push = Math.min(bounds, (minDistance - distance) * 0.55)
    next[id] = { x: point.x + (dx / distance) * push, y: point.y + (dy / distance) * push }
  }
  return next
}
