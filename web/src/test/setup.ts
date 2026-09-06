import { afterEach, vi } from 'vitest'
import { cleanup } from '@testing-library/react'
import '@testing-library/jest-dom/vitest'
import { createMockCore, cytoscapeMock } from './cytoscape-mock'

vi.mock('cytoscape', () => ({
  default: (options: { elements?: Array<{ data: Record<string, unknown> }> }) => createMockCore(options),
}))

afterEach(() => {
  cleanup()
  cytoscapeMock.reset()
  vi.unstubAllGlobals()
})
