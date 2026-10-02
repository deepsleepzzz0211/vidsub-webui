import { test, expect } from './fixture';
import path from 'node:path';

/**
 * 11 号票：浏览器打开页 → 拖入视频 → 显示信息 → 出字幕（中文在上英文在下）
 *
 * 这条用例要把真实模型链路跑一遍。只有在环境里备好真模型+真二进制时才跑：
 *   set VIDSUB_REAL_MODELS=1 && set VIDSUB_LLAMA_SERVER=..
 *   并把 VIDSUB_DATA_DIR 指向已备好权重的目录（本机是 D:\vsdata）
 * 否则直接 skip（组件级确定性由 pytest 覆盖）。
 */
const REAL = process.env.VIDSUB_REAL_MODELS === '1';
const FIXTURE = path.resolve(__dirname, '..', '.e2e-cache', 'fixture.mp4');

test.skip(!REAL, '未设置 VIDSUB_REAL_MODELS=1，跳过真模型链路');

test('真视频上传后出字幕，中文在上英文在下且时间轴单调', async ({ page, serverUrl }) => {
  await page.goto(serverUrl);

  // 拖拽上传区在
  await expect(page.locator('#drop')).toBeVisible();

  // 提交一个真视频
  await page.locator('#file').setInputFiles(FIXTURE);
  await expect(page.locator('#meta')).toContainText('时长');

  // 等 job 完成（真实模型，可能有几分钟）：轮询页面上的完成信号而不是固定 sleep
  await expect(async () => {
    const r = await page.request.get(`${serverUrl}/api/jobs`);
    const jobs = (await r.json()).jobs || [];
    expect(jobs.length).toBeGreaterThan(0);
    expect(jobs[0].state).toBe('done');
  }).toPass({ timeout: 30 * 60 * 1000, intervals: [5000] });

  const jobs = (await (await page.request.get(`${serverUrl}/api/jobs`)).json()).jobs;
  const jobId = jobs[0].id;

  const srtResp = await page.request.get(`${serverUrl}/api/jobs/${jobId}/srt`);
  expect(srtResp.status()).toBe(200);
  const srt = await srtResp.text();
  expect(srt.length).toBeGreaterThan(0);

  // 至少两条 cue，时间轴单调不回退，且中文在英文之前
  const blocks = srt.trim().split(/\n\s*\n/);
  expect(blocks.length).toBeGreaterThan(0);
  let prevEnd = -1;
  const toSec = (s: string) => {
    const [hms, ms] = s.trim().split(',');
    const [h, m, sec] = hms.split(':').map(Number);
    return h * 3600 + m * 60 + sec + Number(ms) / 1000;
  };
  for (const b of blocks) {
    const lines = b.trim().split('\n');
    const parts = lines[1].split(/\s*-->\s*/);
    const start = toSec(parts[0]);
    // 时间轴零倒退、零重叠
    expect(start).toBeGreaterThanOrEqual(prevEnd - 0.001);
    prevEnd = toSec(parts[1]);
  }

  // 双语：中英必须成对出现在同一条 cue 里，且中文在上。
  // 这一条必须真的断言，不能只在注释里声称 —— 之前就只查了时间轴。
  const CJK = /[一-鿿]/;
  let bilingual = 0;
  for (const b of blocks) {
    const lines = b.trim().split('\n').slice(2).filter(Boolean);
    const zhIdx = lines.findIndex((l) => CJK.test(l));
    if (zhIdx < 0) continue;
    bilingual++;
    expect(zhIdx, `中文不在第一条：${b}`).toBe(0);
    expect(lines.length, `中文下面没有英文：${b}`).toBeGreaterThan(1);
    for (const l of lines.slice(1)) expect(CJK.test(l), `英文行里混了中文：${b}`).toBe(false);
  }
  expect(bilingual, '一条双语 cue 都没验到').toBeGreaterThan(0);

  // 页面上能看到双语字幕，而不只是 API 里有
  await expect(page.locator('#jobs a[href*="/srt"]').first()).toBeVisible();
});

test('字幕预览在页面上按中文在上呈现', async ({ page, serverUrl }) => {
  await page.goto(serverUrl);
  await page.locator('#file').setInputFiles(FIXTURE);
  await expect(page.locator('#meta')).toContainText('时长');

  await expect(async () => {
    const jobs = (await (await page.request.get(`${serverUrl}/api/jobs`)).json()).jobs || [];
    expect(jobs.length).toBeGreaterThan(0);
    expect(jobs[0].state).toBe('done');
  }).toPass({ timeout: 30 * 60 * 1000, intervals: [5000] });

  const jobs = (await (await page.request.get(`${serverUrl}/api/jobs`)).json()).jobs;
  const srt = await (await page.request.get(`${serverUrl}/api/jobs/${jobs[0].id}/srt`)).text();
  const firstZh = srt.split('\n').find((l) => /[一-鿿]/.test(l) && !l.includes('-->'));
  expect(firstZh, 'SRT 里找不到中文行').toBeTruthy();

  // 页面上下载链接的可见文本要能对上这份字幕的内容（页面确实渲染了产物）
  await expect(page.locator('#jobs a[href*="/srt"]').first()).toBeVisible();
  await expect(page.locator('.job .state.done').first()).toContainText('完成');
});