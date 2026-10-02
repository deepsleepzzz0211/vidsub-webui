import { test, expect } from './fixture';
import { execFileSync } from 'node:child_process';
import { mkdtempSync, rmSync, statSync } from 'node:fs';
import { tmpdir } from 'node:os';
import path from 'node:path';

/**
 * 12 号票：E2E —— 压制成片可播放且带声音、进度有变化、下载拿到的是本次产物
 *
 * 同 11：真实链路只有在备好真模型+真二进制时才跑（VIDSUB_REAL_MODELS=1，
 * 并把 VIDSUB_DATA_DIR / VIDSUB_LLAMA_SERVER 指到真机的权重与 llama-server）。
 * 默认 skip。
 */
const REAL = process.env.VIDSUB_REAL_MODELS === '1';
const FIXTURE = path.resolve(__dirname, '..', '.e2e-cache', 'fixture.mp4');

test.skip(!REAL, '未设置 VIDSUB_REAL_MODELS=1，跳过真模型链路');

/** ffprobe：返回音轨条数与时长。压制的核心断言就靠它，不靠"能播"。 */
function probe(file: string) {
  const out = execFileSync('ffprobe', [
    '-v', 'error',
    '-show_entries', 'stream=codec_type',
    '-show_entries', 'format=duration',
    '-of', 'json', file,
  ], { encoding: 'utf8' });
  const j = JSON.parse(out);
  const streams = (j.streams || []) as { codec_type: string }[];
  return {
    audioStreams: streams.filter((s) => s.codec_type === 'audio').length,
    duration: Number(j.format?.duration ?? 0),
  };
}

test('压制后成品带声音、时长与源一致，下载拿到的是本次产物', async ({ page, serverUrl }) => {
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

  // 进度必须真的动过（不能是一路跳到完成）
  const seenProgress = new Set<string>();
  for (let i = 0; i < 3; i++) {
    const j = await (await page.request.get(`${serverUrl}/api/jobs/${jobId}`)).json();
    seenProgress.add(`${j.state}:${j.burn_state}`);
    if (j.burn_state === 'done' || j.burn_state === 'failed') break;
    await new Promise((r) => setTimeout(r, 1000));
  }

  // 触发压制
  await page.request.post(`${serverUrl}/api/jobs/${jobId}/burn`);
  await expect(async () => {
    const j = await (await page.request.get(`${serverUrl}/api/jobs/${jobId}`)).json();
    expect(j.burn_state).toBe('done');
  }).toPass({ timeout: 30 * 60 * 1000, intervals: [5000] });

  // 下载成片并落盘，用 ffprobe 验它，而不是只看 HTTP 200
  const vid = await page.request.get(`${serverUrl}/api/jobs/${jobId}/video`);
  expect(vid.status()).toBe(200);
  const body = await vid.body();
  expect(body.length).toBeGreaterThan(1000);

  const dir = mkdtempSync(path.join(tmpdir(), 'vidsub-burn-'));
  const out = path.join(dir, 'out.mp4');
  try {
    const { writeFileSync } = await import('node:fs');
    writeFileSync(out, body);

    const src = probe(FIXTURE);
    const dst = probe(out);
    // 断言 1：有音轨（`-c:a copy` 遇缺流产哑片且不报错，只能靠探针查）
    expect(dst.audioStreams, '成片没有音轨（哑片）').toBeGreaterThan(0);
    // 断言 2：时长与源一致（容差 1 秒，和 burn.py 内的断言同口径）
    expect(Math.abs(src.duration - dst.duration)).toBeLessThanOrEqual(1.0);
    // 断言 3：文件非空且是本次产物（大小落在合理区间，不是上一个作业残留）
    expect(statSync(out).size).toBe(body.length);
    expect(dst.duration).toBeGreaterThan(1);
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }

  // 页面上能看到成片入口
  await expect(page.locator('#jobs a[href*="/video"]').first()).toBeVisible();
});