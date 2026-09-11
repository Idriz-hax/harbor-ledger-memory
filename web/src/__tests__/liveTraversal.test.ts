import { describe, expect, it, vi } from 'vitest'
import { LiveTraversalController } from '../liveTraversal'
import { createMockCore, cytoscapeMock } from '../test/cytoscape-mock'

describe('LiveTraversalController', () => {
  it('keeps concurrent read and write traces independent', () => {
    const core = createMockCore({ elements: [
      { data: { id: 'one' } }, { data: { id: 'two' } },
      { data: { id: 'lane', source: 'one', target: 'two' } },
    ] })
    const controller = new LiveTraversalController(core as never, { reducedMotion: true })
    controller.apply({ trace_id: 'a', sequence: 1, mode: 'read', node_path: 'one' })
    controller.apply({ trace_id: 'b', sequence: 1, mode: 'write', node_path: 'two', source_path: 'one', target_path: 'two' })
    expect(cytoscapeMock.classesFor('one')).toContain('traversal-read')
    expect(cytoscapeMock.classesFor('two')).toContain('traversal-write')
    expect(cytoscapeMock.classesFor('lane')).toContain('traversal-forward')
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
