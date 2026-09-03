// NOTE: not executed on the development machine -- Node/npm are not installed there.
// This configuration is written from the documented Vite defaults and has NOT been run.
import path from 'node:path'

import react from '@vitejs/plugin-react'
import { defineConfig } from 'vite'

export default defineConfig({
  plugins: [react()],
  resolve: {
    alias: { '@': path.resolve(__dirname, './src') },
  },
  server: {
    port: 5173,
    // The backend origin is read from VITE_API_BASE_URL; the proxy keeps the browser on a
    // single origin during development so no CORS preflight is involved.
    proxy: {
      '/api': {
        target: process.env.VITE_API_PROXY_TARGET ?? 'http://localhost:8000',
        changeOrigin: true,
      },
    },
  },
})
