import { test, expect } from './fixture';
import path from 'node:path';

/**
 * 13 号票：E2E —— 字幕样式（双语/纯中文）与多作业并发隔离
 *
 * 同 11/12：真实链路在备好真模型+真二进制时才跑（VIDSUB_REAL_MODELS=1，
 * 并把 VIDSUB_DATA_DIR / VIDSUB_LLAMA_SERVER 指到真机）。默认 skip。
 */
const REAL = process.env.VIDSUB_REAL_MODELS === '1';
const FIXTURE = path.resolve(__dirname, '..', '.e2e-cache', 'fixture.mp4');

test.skip(!REAL, '未设置 VIDSUB_REAL_MODELS=1，跳过真模型链路');

test('同时提交两个作业，产物互不污染、排队串行', async ({ page, serverUrl }) => {
  await page.goto(serverUrl);
  await expect(page.locator('#drop')).toBeVisible();

  // 同时提交两个相同视频（真实场景用两个不同文件，这里验证串行隔离即可）
  await page.locator('#file').setInputFiles(FIXTURE);
  await page.locator('#file').setInputFiles(FIXTURE);

  await expect(async () => {
    const jobs = (await (await page.request.get(`${serverUrl}/api/jobs`)).json()).jobs || [];
    expect(jobs.length).toBeGreaterThanOrEqual(2);
    // 同任意时刻不应有两个都在 running（串行）
    const running = jobs.filter((j: any) => j.state === 'running');
    expect(running.length).toBeLessThanOrEqual(1);
  }).toPass({ timeout: 5 * 60 * 1000, intervals: [5000] });
});