import { describe, expect, it, vi } from 'vitest'
import { LiveTraversalController, MAX_PENDING_EVENTS, MAX_SEQUENCE_TOMBSTONES, SEQUENCE_TOMBSTONE_TTL } from '../liveTraversal'
import { createMockCore, cytoscapeMock } from '../test/cytoscape-mock'

describe('LiveTraversalController', () => {
  it('advances queued events for one trace at 350ms intervals', () => {
    vi.useFakeTimers()
    const core = createMockCore({ elements: [{ data: { id: 'one' } }, { data: { id: 'two' } }] })
    const controller = new LiveTraversalController(core as never)
    controller.apply({ trace_id: 'a', sequence: 1, mode: 'read', node_path: 'one' })
    controller.apply({ trace_id: 'a', sequence: 2, mode: 'read', node_path: 'two' })

    expect(cytoscapeMock.classesFor('one')).not.toContain('traversal-read')
    vi.advanceTimersByTime(350)
    expect(cytoscapeMock.classesFor('one')).toContain('traversal-read')
    expect(cytoscapeMock.classesFor('two')).not.toContain('traversal-read')
    vi.advanceTimersByTime(350)
    expect(cytoscapeMock.classesFor('two')).toContain('traversal-read')
    controller.dispose()
    vi.useRealTimers()
  })

  it('round-robins traces and collapses bounded overflow to newest event', () => {
    vi.useFakeTimers()
    const core = createMockCore({ elements: [
      { data: { id: 'a1' } }, { data: { id: 'a2' } }, { data: { id: 'a3' } }, { data: { id: 'newest' } },
      { data: { id: 'b1' } },
    ] })
    const controller = new LiveTraversalController(core as never)
    controller.apply({ trace_id: 'a', sequence: 1, mode: 'read', node_path: 'a1' })
    controller.apply({ trace_id: 'b', sequence: 1, mode: 'write', node_path: 'b1' })

    vi.advanceTimersByTime(350)
    expect(cytoscapeMock.classesFor('a1')).toContain('traversal-read')
    controller.apply({ trace_id: 'a', sequence: 2, mode: 'read', node_path: 'a2' })
    vi.advanceTimersByTime(350)
    expect(cytoscapeMock.classesFor('b1')).toContain('traversal-write')
    vi.advanceTimersByTime(350)
    expect(cytoscapeMock.classesFor('a2')).toContain('traversal-read')
    controller.apply({ trace_id: 'a', sequence: 3, mode: 'read', node_path: 'a3' })
    controller.apply({ trace_id: 'a', sequence: 4, mode: 'read', node_path: 'newest' })
    controller.apply({ trace_id: 'a', sequence: 5, mode: 'read', node_path: 'newest' })
    controller.apply({ trace_id: 'a', sequence: 6, mode: 'read', node_path: 'newest' })
    vi.advanceTimersByTime(500)
    expect(cytoscapeMock.classesFor('a3')).not.toContain('traversal-read')
    expect(cytoscapeMock.classesFor('a1')).not.toContain('traversal-read')
    expect(cytoscapeMock.classesFor('newest')).toContain('traversal-read')
    controller.dispose()
    vi.useRealTimers()
  })

  it('keeps halos visible for 1.2 seconds and reports active traces', () => {
    vi.useFakeTimers()
    const core = createMockCore({ elements: [{ data: { id: 'one' } }] })
    const controller = new LiveTraversalController(core as never)
    controller.apply({ trace_id: 'a', sequence: 1, mode: 'read', node_path: 'one' })
    expect(controller.activeTraceCount()).toBe(1)
    vi.advanceTimersByTime(350)
    expect(cytoscapeMock.classesFor('one')).toContain('traversal-read')
    vi.advanceTimersByTime(1199)
    expect(cytoscapeMock.classesFor('one')).toContain('traversal-read')
    vi.advanceTimersByTime(1)
    expect(cytoscapeMock.classesFor('one')).not.toContain('traversal-read')
    expect(controller.activeTraceCount()).toBe(0)
    controller.dispose()
    vi.useRealTimers()
  })

  it('notifies the live status when a trace starts and its halo clears', () => {
    vi.useFakeTimers()
    const core = createMockCore({ elements: [{ data: { id: 'one' } }] })
    const activity = vi.fn()
    const controller = new LiveTraversalController(core as never, { onActivityChange: activity })

    controller.apply({ trace_id: 'a', sequence: 1, mode: 'read', node_path: 'one' })
    expect(activity).toHaveBeenLastCalledWith(1)
    vi.advanceTimersByTime(1550)
    expect(activity).toHaveBeenLastCalledWith(0)
    expect(activity).toHaveBeenCalledTimes(2)
    controller.dispose()
    vi.useRealTimers()
  })

  it('keeps reduced-motion color sequencing while disabling edge animation', () => {
    vi.useFakeTimers()
    const core = createMockCore({ elements: [
      { data: { id: 'one' } }, { data: { id: 'two' } },
      { data: { id: 'edge', source: 'one', target: 'two', edge_type: 'links_to' } },
    ] })
    const controller = new LiveTraversalController(core as never, { reducedMotion: true })
    controller.apply({ trace_id: 'a', sequence: 1, mode: 'read', node_path: 'one', source_path: 'one', target_path: 'two', edge_type: 'links_to' })
    controller.apply({ trace_id: 'a', sequence: 2, mode: 'write', node_path: 'two' })
    vi.advanceTimersByTime(350)
    expect(cytoscapeMock.classesFor('one')).toContain('traversal-read')
    expect(cytoscapeMock.animations).toHaveLength(0)
    vi.advanceTimersByTime(350)
    expect(cytoscapeMock.classesFor('two')).toContain('traversal-write')
    controller.dispose()
    vi.useRealTimers()
  })

  it('colors write traversal edges amber independently from read edges', () => {
    vi.useFakeTimers()
    const core = createMockCore({ elements: [
      { data: { id: 'one' } }, { data: { id: 'two' } },
      { data: { id: 'edge', source: 'one', target: 'two', edge_type: 'links_to' } },
    ] })
    const controller = new LiveTraversalController(core as never, { reducedMotion: true })

    controller.apply({ trace_id: 'write', sequence: 1, mode: 'write', node_path: 'two', source_path: 'one', target_path: 'two', edge_type: 'links_to' })
    vi.advanceTimersByTime(350)
    expect(cytoscapeMock.classesFor('edge')).toContain('traversal-forward-write')
    expect(cytoscapeMock.classesFor('edge')).not.toContain('traversal-forward-read')
    controller.dispose()
    vi.useRealTimers()
  })

  it('keeps concurrent read and write traces independent', () => {
    vi.useFakeTimers()
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
    vi.advanceTimersByTime(700)
    expect(cytoscapeMock.classesFor(sourceId)).toContain('traversal-read')
    expect(cytoscapeMock.classesFor(targetId)).toContain('traversal-write')
    expect(cytoscapeMock.classesFor(edgeId)).toContain('traversal-forward-write')
  })

  it('ignores stale sequences and removes only the disposed trace classes', () => {
    vi.useFakeTimers()
    const core = createMockCore({ elements: [{ data: { id: 'one' } }] })
    const controller = new LiveTraversalController(core as never)
    controller.apply({ trace_id: 'a', sequence: 2, mode: 'read', node_path: 'one' })
    controller.apply({ trace_id: 'a', sequence: 1, mode: 'write', node_path: 'one' })
    vi.advanceTimersByTime(1550)
    expect(cytoscapeMock.classesFor('one')).not.toContain('traversal-read')
    expect(cytoscapeMock.classesFor('one')).not.toContain('traversal-write')
    vi.useRealTimers()
  })

  it('suppresses stale completed traces within a bounded tombstone window', () => {
    vi.useFakeTimers()
    const core = createMockCore({ elements: [{ data: { id: 'one' } }] })
    const controller = new LiveTraversalController(core as never)

    controller.apply({ trace_id: 'completed', sequence: 2, mode: 'read', node_path: 'one' })
    vi.advanceTimersByTime(1550)
    controller.apply({ trace_id: 'completed', sequence: 1, mode: 'read', node_path: 'one' })
    expect(controller.activeTraceCount()).toBe(0)

    vi.advanceTimersByTime(SEQUENCE_TOMBSTONE_TTL)
    controller.apply({ trace_id: 'completed', sequence: 1, mode: 'read', node_path: 'one' })
    expect(controller.activeTraceCount()).toBe(1)
    expect((controller as unknown as { tombstones: Map<string, unknown> }).tombstones.size).toBeLessThanOrEqual(MAX_SEQUENCE_TOMBSTONES)
    controller.dispose()
    vi.useRealTimers()
  })

  it('does not light an edge when an aggregate path maps ambiguously', () => {
    vi.useFakeTimers()
    const core = createMockCore({ elements: [
      { data: { id: 'cluster-a' } }, { data: { id: 'cluster-b' } },
      { data: { id: 'edge', source: 'cluster-a', target: 'cluster-b', edge_type: 'links_to' } },
    ] })
    const controller = new LiveTraversalController(core as never, {
      nodeIdForPath: () => 'cluster-a',
      edgeIdForEvent: () => undefined,
      reducedMotion: true,
    })
    controller.apply({ trace_id: 'aggregate', sequence: 1, mode: 'read', node_path: 'AI/one.md', source_path: 'AI/one.md', target_path: 'AI/two.md', edge_type: 'links_to' })
    vi.advanceTimersByTime(350)
    expect(cytoscapeMock.classesFor('cluster-a')).toContain('traversal-read')
    expect(cytoscapeMock.classesFor('edge')).not.toContain('traversal-forward')
    controller.dispose()
    vi.useRealTimers()
  })

  it('keeps unresolved events for a slow projection without counting them as active', () => {
    vi.useFakeTimers()
    const activity = vi.fn()
    const core = createMockCore({ elements: [{ data: { id: 'resolved' } }] })
    let mapped = false
    const controller = new LiveTraversalController(core as never, {
      onActivityChange: activity,
      reducedMotion: true,
      nodeIdForPath: () => mapped ? 'resolved' : undefined,
    })

    controller.apply({ trace_id: 'early', sequence: 1, mode: 'read', node_path: 'AI/one.md' })
    vi.advanceTimersByTime(350)
    expect(controller.activeTraceCount()).toBe(0)
    expect(activity).toHaveBeenLastCalledWith(0)

    mapped = true
    controller.flush()
    expect(controller.activeTraceCount()).toBe(1)
    vi.advanceTimersByTime(350)
    expect(cytoscapeMock.classesFor('resolved')).toContain('traversal-read')
    expect(controller.activeTraceCount()).toBe(1)
    vi.advanceTimersByTime(1200)
    expect(controller.activeTraceCount()).toBe(0)
    expect(activity).toHaveBeenLastCalledWith(0)
    controller.dispose()
    vi.useRealTimers()
  })

  it('retains the latest renderable event when overflow ends with an unmappable event', () => {
    vi.useFakeTimers()
    const core = createMockCore({ elements: [
      { data: { id: 'first' } }, { data: { id: 'latest-renderable' } },
    ] })
    const controller = new LiveTraversalController(core as never, { reducedMotion: true })

    controller.apply({ trace_id: 'overflow', sequence: 1, mode: 'read', node_path: 'first' })
    controller.apply({ trace_id: 'overflow', sequence: 2, mode: 'read', node_path: 'latest-renderable' })
    controller.apply({ trace_id: 'overflow', sequence: 3, mode: 'read', node_path: 'missing-a' })
    controller.apply({ trace_id: 'overflow', sequence: 4, mode: 'read', node_path: 'missing-b' })
    vi.advanceTimersByTime(350)

    expect(cytoscapeMock.classesFor('first')).not.toContain('traversal-read')
    expect(cytoscapeMock.classesFor('latest-renderable')).toContain('traversal-read')
    controller.dispose()
    vi.useRealTimers()
  })

  it('evicts oldest unresolved events globally and never replays their stale entries', () => {
    vi.useFakeTimers()
    const traceIds = Array.from({ length: MAX_PENDING_EVENTS + MAX_SEQUENCE_TOMBSTONES + 1 }, (_, index) => `pending-${index}`)
    const core = createMockCore({ elements: traceIds.map(id => ({ data: { id: `node-${id}` } })) })
    let mapped = false
    const controller = new LiveTraversalController(core as never, {
      reducedMotion: true,
      nodeIdForPath: path => mapped ? `node-${path}` : undefined,
    })

    traceIds.forEach((traceId, sequence) => controller.apply({ trace_id: traceId, sequence: 1, mode: 'read', node_path: traceId }))
    vi.advanceTimersByTime(350 * (traceIds.length + 5))
    expect(controller.activeTraceCount()).toBe(0)
    expect((controller as unknown as { tombstones: Map<string, unknown> }).tombstones.size).toBeLessThanOrEqual(MAX_SEQUENCE_TOMBSTONES)

    // The projection resolves all remaining pending traces; the oldest one was evicted.
    mapped = true
    controller.flush()
    expect(controller.activeTraceCount()).toBe(MAX_PENDING_EVENTS)
    vi.advanceTimersByTime(350)
    expect(controller.activeTraceCount()).toBe(MAX_PENDING_EVENTS)
    controller.apply({ trace_id: traceIds[MAX_SEQUENCE_TOMBSTONES], sequence: 1, mode: 'read', node_path: traceIds[MAX_SEQUENCE_TOMBSTONES] })
    expect(controller.activeTraceCount()).toBe(MAX_PENDING_EVENTS)
    controller.dispose()
    vi.useRealTimers()
  })

  it('buffers an event until its node mapping becomes available', () => {
    vi.useFakeTimers()
    const core = createMockCore({ elements: [{ data: { id: 'resolved' } }] })
    let mapped = false
    const controller = new LiveTraversalController(core as never, {
      reducedMotion: true,
      nodeIdForPath: () => mapped ? 'resolved' : undefined,
    })

    controller.apply({ trace_id: 'early', sequence: 1, mode: 'read', node_path: 'AI/Harbor.md' })
    expect(cytoscapeMock.classesFor('resolved')).not.toContain('traversal-read')

    mapped = true
    controller.flush()
    vi.advanceTimersByTime(350)
    expect(cytoscapeMock.classesFor('resolved')).toContain('traversal-read')
    controller.dispose()
    vi.useRealTimers()
  })
})
