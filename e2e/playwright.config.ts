import { defineConfig } from '@playwright/test'

export default defineConfig({
  testDir: './tests',
  timeout: 700_000,        // 12 min per test — queue from API tests may be ahead of us
  expect: { timeout: 10_000 },
  workers: 1,              // serial: pipeline jobs are CPU/RAM heavy
  retries: 0,
  reporter: [['list'], ['html', { open: 'never', outputFolder: 'playwright-report' }]],
  use: {
    baseURL: 'http://localhost',
    headless: true,
    screenshot: 'only-on-failure',
    video: 'retain-on-failure',
    trace: 'retain-on-failure',
  },
  outputDir: 'test-results',
})
