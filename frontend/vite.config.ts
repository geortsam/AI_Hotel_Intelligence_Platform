import { fileURLToPath } from 'node:url'

import react from '@vitejs/plugin-react'
import { defineConfig } from 'vite'

export default defineConfig({
  plugins: [react()],
  resolve: {
    // `__dirname` does not exist in an ES module, and package.json sets "type": "module",
    // so this file is one. Resolving against `import.meta.url` is the ESM equivalent, and
    // it is what makes the `@/` alias work at all.
    alias: { '@': fileURLToPath(new URL('./src', import.meta.url)) },
  },
  server: {
    port: 5173,
    // Development convenience only. With VITE_API_BASE_URL left empty the browser talks to
    // the dev server's own origin, this proxy forwards /api to the backend, and no CORS
    // preflight is involved. A deployed build sets VITE_API_BASE_URL and never uses this.
    proxy: {
      '/api': {
        target: process.env.VITE_API_PROXY_TARGET ?? 'http://localhost:8000',
        changeOrigin: true,
      },
    },
  },
})
