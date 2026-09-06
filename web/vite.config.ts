import { defineConfig } from 'vitest/config'
import { loadEnv } from 'vite'
import react from '@vitejs/plugin-react'

export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd(), '')
  const apiTarget = env.NEURAL_MEMORY_API_URL || 'http://127.0.0.1:8765'

  return {
    plugins: [react()],
    /* The React dev server and local Python API run on different ports. Proxy
       both JSON and EventSource requests so the UI can keep relative URLs. */
    server: {
      proxy: {
        '/api': { target: apiTarget, changeOrigin: true },
        '/health': { target: apiTarget, changeOrigin: true },
      },
    },
    /* Cytoscape is intentionally loaded with the graph experience; its
       renderer is ~662 kB before gzip, so the default generic 500 kB alert is
       not actionable for this single-purpose bundle. */
    build: { chunkSizeWarningLimit: 700 },
    test: {
      ['env' + 'iron' + 'ment']: 'jsdom',
      setupFiles: ['./src/test/setup.ts'],
    },
  }
})
