/// <reference types="vitest" />
import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';
import { configDefaults } from 'vitest/config';

// The dashboard SPA talks to the orchestrator API through a same-origin `/api`
// prefix; in dev the Vite server proxies that to the locally-running FastAPI
// (uvicorn swebench_eval.orchestrator.api.main:app, port 8000 — the
// orchestrator image's exported port).  In production the static site sits
// behind a reverse proxy that forwards `/api` to the same FastAPI.  Nothing in
// the app ever talks to a different host, so there is exactly one seam to swap.
export default defineConfig(({ command }) => ({
  // The demo build (VITE_DATA_MODE=demo) is served from the domain root of a
  // static host; data is fetched with relative URLs so the same build also
  // works under a subpath. Explicit here so the seam is visible in one place.
  // The LIVE build is served by the orchestrator API itself under /ui/ (adoption Phase 2,
  // option 2 — infra/docker/Dockerfile.orchestrator builds it, main.py mounts it), so its
  // assets resolve under that prefix; `/api` is forwarded in-process by the same app. The
  // dev server keeps the root (Vite proxies /api itself).
  base: process.env.VITE_DATA_MODE === 'demo' ? '/' : command === 'build' ? '/ui/' : undefined,
  plugins: [react()],
  server: {
    proxy: {
      '/api': {
        target: 'http://127.0.0.1:8000',
        changeOrigin: true,
        rewrite: (path) => path.replace(/^\/api/, ''),
      },
    },
  },
  test: {
    environment: 'jsdom',
    setupFiles: ['./vitest.setup.ts'],
    // The five phase-2 tests are correctness rules (§4a), not render checks —
    // they must never silently pass because a component failed to mount.
    globals: false,
    // e2e/*.spec.ts are Playwright specs (playwright.config.ts), not vitest.
    exclude: [...configDefaults.exclude, 'e2e/**'],
  },
}));
