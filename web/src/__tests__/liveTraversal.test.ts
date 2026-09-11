import { describe, expect, it, vi } from 'vitest'
import { LiveTraversalController } from '../liveTraversal'
import { createMockCore, cytoscapeMock } from '../test/cytoscape-mock'

describe('LiveTraversalController', () => {
  it('keeps concurrent read and write traces independent', () => {
    const sourceId = 'c_965a365f830b12ad'
    const targetId = 'c_593d52059e6dd33e'
    const edgeId = 'a9d88e29efcc3cbb'
    const core = createMockCore({ elements: [
      { data: { id: sourceId } }, { data: { id: targetId } },
      { data: { id: edgeId, source: sourceId, target: targetId, edge_type: 'links_to' } },
    ] })
    const controller = new LiveTraversalController(core as never, {
      reducedMotion: true,
      nodeIdForPath: path => path === 'one.md' ? sourceId : path === 'two.md' ? targetId : undefined,
      edgeIdForEvent: event => event.edge_type === 'links_to' ? edgeId : undefined,
    })
    controller.apply({ trace_id: 'a', sequence: 1, mode: 'read', node_path: 'one.md' })
    controller.apply({ trace_id: 'b', sequence: 1, mode: 'write', node_path: 'two.md', source_path: 'one.md', target_path: 'two.md', edge_type: 'links_to' })
    expect(cytoscapeMock.classesFor(sourceId)).toContain('traversal-read')
    expect(cytoscapeMock.classesFor(targetId)).toContain('traversal-write')
    expect(cytoscapeMock.classesFor(edgeId)).toContain('traversal-forward')
  })

  it('ignores stale sequences and removes only the disposed trace classes', () => {
    vi.useFakeTimers()
    const core = createMockCore({ elements: [{ data: { id: 'one' } }] })
    const controller = new LiveTraversalController(core as never)
    controller.apply({ trace_id: 'a', sequence: 2, mode: 'read', node_path: 'one' })
    controller.apply({ trace_id: 'a', sequence: 1, mode: 'write', node_path: 'one' })
    vi.advanceTimersByTime(900)
    expect(cytoscapeMock.classesFor('one')).not.toContain('traversal-read')
    expect(cytoscapeMock.classesFor('one')).not.toContain('traversal-write')
    vi.useRealTimers()
  })
})
