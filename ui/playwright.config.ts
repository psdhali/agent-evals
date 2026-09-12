import { defineConfig } from '@playwright/test';

// One committed spec that turns the phase-2 manual pass into a command
// (builder2-operator-dashboard-phase2.md §5).  Runs against the LOCAL stack:
// the Vite dev server (this config starts it) proxying /api to a uvicorn
// orchestrator API on :8000 that the operator already has running
// (docker compose up + the API started with the SQS/S3 endpoint env vars).
//
//   npx playwright test            # against localhost:5173
//
// Not run in CI (ci.yml has no node/playwright job); it is the human's
// repeatable replacement for hand-driving Playwright.

export default defineConfig({
  testDir: './e2e',
  timeout: 30_000,
  fullyParallel: false,
  retries: 0,
  use: {
    baseURL: 'http://localhost:5173',
    trace: 'retain-on-failure',
  },
  webServer: {
    command: 'npm run dev',
    url: 'http://localhost:5173',
    reuseExistingServer: true,
    timeout: 60_000,
  },
  reporter: [['list']],
});
