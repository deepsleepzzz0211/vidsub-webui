import { defineConfig, devices } from '@playwright/test';

/**
 * 端口由 Python 夹具随机挑选（避免与开发者本机在跑的实例冲突），
 * 因此这里不设 baseURL —— 实际的 URL 由 e2e/fixture.ts 注入。
 */
export default defineConfig({
  testDir: './e2e',
  // 真实推理跑一遍要几分钟，超时要给足
  timeout: 300_000,
  expect: { timeout: 20_000 },
  fullyParallel: false,          // CPU 推理是独占的，别并行抢
  workers: 1,
  use: {
    headless: true,
    trace: 'retain-on-failure',
    screenshot: 'only-on-failure',
    video: 'off',
  },
  projects: [
    { name: 'chromium', use: { ...devices['Desktop Chrome'] } },
  ],
  // 跳过原因必须看得见：list 报告器不打印 skip 的理由，
  // 素材下载失败时会变成"一片绿 + 零线索"，最容易被误读成"没问题"。
  reporter: [['list'], ['html', { open: 'never' }]],
});
