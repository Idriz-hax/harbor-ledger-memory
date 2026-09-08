import { describe, expect, it } from 'vitest'
import { stablePosition, viewLevelForZoom } from '../graphView'

describe('Tide Atlas bounded graph view', () => {
  it('uses hysteresis: files at high zoom, coast at low zoom, dead zone in between', () => {
    /* From level 2 (files): only a much farther zoom-out switches to coast */
    expect(viewLevelForZoom(0.1, false, 2)).toBe(0)
    expect(viewLevelForZoom(0.2, false, 2)).toBe(2)
    expect(viewLevelForZoom(0.8, false, 2)).toBe(2)
    /* From level 0 (coast): zoom in above 0.5 to switch to files */
    expect(viewLevelForZoom(0.6, false, 0)).toBe(2)
    expect(viewLevelForZoom(0.48, false, 0)).toBe(0)
    expect(viewLevelForZoom(0.2, false, 0)).toBe(0)
    /* No current level: default threshold */
    expect(viewLevelForZoom(0.1, false)).toBe(0)
    expect(viewLevelForZoom(0.8, false)).toBe(2)
  })

  it('keeps cluster placement stable across refreshes', () => {
    expect(stablePosition('Knowledge:Index', 2, 8)).toEqual(stablePosition('Knowledge:Index', 2, 8))
    expect(stablePosition('Knowledge:Index', 2, 8)).not.toEqual(stablePosition('Projects:File', 3, 8))
  })
})
