import { describe, expect, it } from 'vitest'
import { buildCoastalPlacement, isIndexPath, settleOneHop } from '../coastalLayout'

const nodes = [
  { id: 'AI:Index', scope: 'AI/Index.md', kind: 'index' },
  { id: 'AI:Note', scope: 'AI/Note.md', kind: 'file' },
  { id: 'Projects:Loose', scope: 'Projects/Loose.md', kind: 'file' },
]

describe('buildCoastalPlacement', () => {
  it('returns repeatable compact positions for the same graph', () => {
    const first = buildCoastalPlacement(nodes, [{ source: 'AI:Index', target: 'AI:Note' }], {})
    const second = buildCoastalPlacement(nodes, [{ source: 'AI:Index', target: 'AI:Note' }], {})
    expect(first).toEqual(second)
    expect(Math.max(...Object.values(first.positions).map(({ x, y }) => Math.hypot(x, y)))).toBeLessThan(900)
  })

  it('changes seeded positions when given a different layout seed', () => {
    const first = buildCoastalPlacement(nodes, [{ source: 'AI:Index', target: 'AI:Note' }], {}, 'first')
    const second = buildCoastalPlacement(nodes, [{ source: 'AI:Index', target: 'AI:Note' }], {}, 'second')

    expect(Object.keys(first.positions).some(id => first.positions[id].x !== second.positions[id].x || first.positions[id].y !== second.positions[id].y)).toBe(true)
  })

  it('keeps linked notes in a compact field around their island center', () => {
    const layout = buildCoastalPlacement(nodes, [{ source: 'AI:Index', target: 'AI:Note' }], {})
    const ai = layout.islands.find(island => island.key === 'AI')!
    expect(Math.hypot(layout.positions['AI:Note'].x - ai.center.x, layout.positions['AI:Note'].y - ai.center.y))
      .toBeLessThan(166)
  })

  it('uses a saved drag override before its deterministic seed', () => {
    const layout = buildCoastalPlacement(nodes, [], { 'AI:Note': { x: 24, y: -18 } })
    expect(layout.positions['AI:Note']).toEqual({ x: 24, y: -18 })
  })

  it('groups slashless scopes into the root roadstead', () => {
    const layout = buildCoastalPlacement([
      { id: 'Index', scope: 'Index.md', kind: 'index' },
      { id: 'README', scope: 'README.md', kind: 'file' },
    ], [], {})

    expect(layout.islands).toHaveLength(1)
    expect(layout.islands[0]).toMatchObject({ key: '(root)', nodeIds: ['Index', 'README'] })
  })

  it('settles only the moved node’s immediate neighbours', () => {
    const result = settleOneHop({ moved: { x: 0, y: 0 }, near: { x: 10, y: 0 }, far: { x: 100, y: 0 } }, [
      { source: 'moved', target: 'near' },
      { source: 'near', target: 'far' },
    ], 'moved')
    expect(result.near.x).toBeGreaterThan(10)
    expect(result.far).toEqual({ x: 100, y: 0 })
  })

  it('puts a node at the local center and derives the island envelope from its field', () => {
    const layout = buildCoastalPlacement([
      { id: 'a', scope: 'AI/a.md', kind: 'file' },
      { id: 'b', scope: 'AI/b.md', kind: 'file' },
      { id: 'c', scope: 'AI/c.md', kind: 'file' },
    ], [], {})
    const island = layout.islands[0]
    expect(island.radius).toBeGreaterThan(166 + 34)
    expect(layout.positions.a).toEqual(island.center)
    expect(new Set(island.nodeIds.map(id => Math.hypot(
      layout.positions[id].x - island.center.x,
      layout.positions[id].y - island.center.y,
    ))).size).toBeGreaterThan(1)
  })

  it('keeps every seeded node in a group at least 48px apart', () => {
    const groupNodes = Array.from({ length: 12 }, (_, index) => ({
      id: `dense-${index}`,
      scope: 'Dense/note.md',
      kind: 'file',
    }))
    const layout = buildCoastalPlacement(groupNodes, [], {})
    const points = groupNodes.map(node => layout.positions[node.id])

    for (let first = 0; first < points.length; first += 1) {
      for (let second = first + 1; second < points.length; second += 1) {
        expect(Math.hypot(points[first].x - points[second].x, points[first].y - points[second].y)).toBeGreaterThanOrEqual(48)
      }
    }
  })

  it('packs group disks from the center without overlap', () => {
    const layout = buildCoastalPlacement(Array.from({ length: 9 }, (_, index) => ({
      id: `group-${index}`,
      scope: `${String.fromCharCode(65 + index)}/note.md`,
      kind: 'file',
    })), [], {})

    const distances = layout.islands.map(island => Math.hypot(island.center.x, island.center.y))
    expect(Math.min(...distances)).toBe(0)
    expect(new Set(distances).size).toBeGreaterThan(1)
    for (let first = 0; first < layout.islands.length; first += 1) {
      for (let second = first + 1; second < layout.islands.length; second += 1) {
        const a = layout.islands[first]
        const b = layout.islands[second]
        expect(Math.hypot(a.center.x - b.center.x, a.center.y - b.center.y))
          .toBeGreaterThanOrEqual(a.radius + b.radius)
      }
    }
  })

  it('classifies only a scope-ending index.md path as an index', () => {
    expect(isIndexPath('AI/INDEX.md')).toBe(true)
    expect(isIndexPath('AI/index.MD')).toBe(true)
    expect(isIndexPath('AI/index.md/archive.md')).toBe(false)
    expect(isIndexPath('AI/index-notes.md')).toBe(false)
  })
})
