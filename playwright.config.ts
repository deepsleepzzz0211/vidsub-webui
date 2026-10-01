import { defineConfig } from '@playwright/test';

export default defineConfig({
  testDir: './e2e',
  timeout: 180_000,
  expect: { timeout: 15_000 },
  use: {
    baseURL: process.env.VIDSUB_E2E_URL || 'http://127.0.0.1:8855',
    headless: true,
    // 失败时保留 trace 与截图，便于定位——E2E 最怕"只在 CI 上挂且没线索"
    trace: 'retain-on-failure',
    screenshot: 'only-on-failure',
  },
  reporter: [['list']],
});
