import { fileURLToPath } from 'node:url'

import react from '@vitejs/plugin-react'
import { defineConfig } from 'vitest/config'

/**
 * Test configuration, separate from `vite.config.ts`.
 *
 * The app config carries a dev-server proxy and a port that mean nothing under a test
 * runner, and merging the two would make each harder to read than keeping the alias in
 * both. The alias is the one thing that must agree, and a test importing `@/...` fails
 * loudly if it ever stops agreeing.
 */
export default defineConfig({
  plugins: [react()],
  resolve: {
    alias: { '@': fileURLToPath(new URL('./src', import.meta.url)) },
  },
  test: {
    environment: 'jsdom',
    globals: true,
    setupFiles: ['./src/test/setup.ts'],
    /*
     * CSS is deliberately NOT processed.
     *
     * jsdom applies plain rules but does not evaluate media queries, so with CSS on, the
     * sidebar's mobile-first `visibility: hidden` always applied and the desktop override
     * never did -- every test saw a permanently closed drawer and could not query the
     * navigation at all. That is not the application's behaviour, it is jsdom's blind spot,
     * and asserting around it would have meant writing tests that agree with a lie.
     *
     * So these tests cover semantics: roles, names, ARIA state, routing. Anything that
     * depends on CSS -- the drawer actually being off-canvas, the desktop sidebar being
     * permanent, responsive reflow -- is verified in a real browser instead, where media
     * queries exist.
     */
    css: false,
    include: ['src/**/*.test.{ts,tsx}'],
  },
})
