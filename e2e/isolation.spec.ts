import { test, expect } from './fixture';
import { existsSync } from 'node:fs';
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

test('两个不同视频各自产出自己的字幕，不串内容，串行不并发', async ({ page, serverUrl }) => {
  await page.goto(serverUrl);
  await expect(page.locator('#drop')).toBeVisible();

  // **必须用两个不同的视频**：提交同一个文件验不出串内容 —— 两份产物
  // 本来就该一模一样，串了也看不出来。
  // 第二个素材取前 40 秒，字幕内容必然不同。
  const clip = path.resolve(__dirname, '..', '.e2e-cache', 'fixture-40s.mp4');
  test.skip(!existsSync(clip), `缺少第二份素材：${clip}`);
  expect(clip).not.toBe(FIXTURE);

  await page.locator('#file').setInputFiles(FIXTURE);
  await page.locator('#file').setInputFiles(clip);

  // 串行：同一时刻最多一个在跑，其余排队
  await expect(async () => {
    const jobs = (await (await page.request.get(`${serverUrl}/api/jobs`)).json()).jobs || [];
    expect(jobs.length).toBeGreaterThanOrEqual(2);
    const running = jobs.filter((j: any) => j.state === 'running');
    expect(running.length, '两个作业同时在跑，串行失效').toBeLessThanOrEqual(1);
  }).toPass({ timeout: 5 * 60 * 1000, intervals: [3000] });

  // 等两个都完成
  await expect(async () => {
    const jobs = (await (await page.request.get(`${serverUrl}/api/jobs`)).json()).jobs || [];
    expect(jobs.filter((j: any) => j.state === 'done').length).toBeGreaterThanOrEqual(2);
  }).toPass({ timeout: 40 * 60 * 1000, intervals: [5000] });

  const jobs = (await (await page.request.get(`${serverUrl}/api/jobs`)).json()).jobs;
  const full = jobs.find((j: any) => j.filename.endsWith('fixture.mp4'));
  const part = jobs.find((j: any) => j.filename.endsWith('fixture-40s.mp4'));
  expect(full, '没找到完整视频的作业').toBeTruthy();
  expect(part, '没找到 40 秒 clip 的作业').toBeTruthy();

  const srtOf = async (j: any) =>
    (await page.request.get(`${serverUrl}/api/jobs/${j.id}/srt`)).text();
  const [a, b] = [await srtOf(full), await srtOf(part)];

  // 串内容 = 两份产物相同。40 秒 clip 只含前 40 秒，字幕必然更短更少。
  expect(a.length).toBeGreaterThan(0);
  expect(b.length).toBeGreaterThan(0);
  expect(b.length, '两个作业的字幕长度一样，疑似串了内容').toBeLessThan(a.length);

  // 且各自的产物落在自己的目录里（产物不共享路径）
  expect(full.srt).not.toBe(part.srt);
  expect(full.work_dir).not.toBe(part.work_dir);
});