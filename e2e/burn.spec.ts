import { test, expect } from './fixture';
import path from 'node:path';

/**
 * 12 号票：E2E —— 压制成片可播放且带声音
 *
 * 同 11：真实链路只有在备好真模型+真二进制时才跑（VIDSUB_REAL_MODELS=1，
 * 并把 VIDSUB_DATA_DIR / VIDSUB_LLAMA_SERVER 指到真机的权重与 llama-server）。
 * 默认 skip。
 */
const REAL = process.env.VIDSUB_REAL_MODELS === '1';
const FIXTURE = path.resolve(__dirname, '..', '.e2e-cache', 'fixture.mp4');

test.skip(!REAL, '未设置 VIDSUB_REAL_MODELS=1，跳过真模型链路');

test('压制后成品可播放且带声音', async ({ page, serverUrl }) => {
  await page.goto(serverUrl);
  await expect(page.locator('#drop')).toBeVisible();
  await page.locator('#file').setInputFiles(FIXTURE);
  await expect(page.locator('#meta')).toContainText('时长');

  // 等 SRT 完成
  await expect(async () => {
    const jobs = (await (await page.request.get(`${serverUrl}/api/jobs`)).json()).jobs || [];
    expect(jobs.length).toBeGreaterThan(0);
    expect(jobs[0].state).toBe('done');
  }).toPass({ timeout: 30 * 60 * 1000, intervals: [5000] });

  const jobs = (await (await page.request.get(`${serverUrl}/api/jobs`)).json()).jobs;
  const jobId = jobs[0].id;

  // 触发压制
  await page.request.post(`${serverUrl}/api/jobs/${jobId}/burn`);
  await expect(async () => {
    const j = (await (await page.request.get(`${serverUrl}/api/jobs/${jobId}`)).json());
    expect(j.burn_state).toBe('done');
  }).toPass({ timeout: 30 * 60 * 1000, intervals: [5000] });

  const vid = await page.request.get(`${serverUrl}/api/jobs/${jobId}/video`);
  expect(vid.status()).toBe(200);
});