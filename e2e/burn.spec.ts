import { test, expect, fixtureFor, runTool } from './fixture';
import { mkdtempSync, rmSync, statSync } from 'node:fs';
import { tmpdir } from 'node:os';
import path from 'node:path';

/**
 * 12 号票：E2E —— 压制成片可播放且带声音、进度有变化、下载拿到的是本次产物
 *
 * 真模型链路，跑法同 11 号票（VIDSUB_REAL_MODELS / VIDSUB_REAL_MODELS_DIR /
 * VIDSUB_LLAMA_SERVER）。没设权重目录就 skip。
 */
test.use({ withModels: true });

const FIXTURE = fixtureFor('fixture.mp4');

// 压制要逐帧烧字幕，真模型下很慢
test.setTimeout(45 * 60 * 1000);

/** ffprobe：返回音轨条数与时长。压制的核心断言就靠它，不靠"能播"。 */
async function probe(file: string) {
  const out = await runTool('ffprobe', [
    '-v', 'error',
    '-show_entries', 'stream=codec_type',
    '-show_entries', 'format=duration',
    '-of', 'json', file,
  ]);
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

  // 提交后**立刻**开始采样进度，一直采到完成。
  //
  // 必须从提交那一刻起采：如果先等 done 再回头看进度，能采到的只有
  // "done" 一个值，"进度会动"这条就永远验不到。工单要的就是
  // "不是一直停在同一状态"。
  const labels: string[] = [];
  const pollUntilDone = async () => {
    for (;;) {
      const list = (await (await page.request.get(`${serverUrl}/api/jobs`)).json()).jobs || [];
      const j = list[0];
      if (j) {
        const label = `${j.state}:${(j.progress || {}).label || ''}`;
        if (labels[labels.length - 1] !== label) labels.push(label);
        if (j.state === 'done' || j.state === 'failed') return j;
      }
      await new Promise((r) => setTimeout(r, 500));
    }
  };

  await expect(async () => {
    const j = await pollUntilDone();
    expect(j.state, `作业卡住了：${j.error || ''}`).toBe('done');
  }).toPass({ timeout: 30 * 60 * 1000, intervals: [1000] });

  // 阶段确实推进过：不只是 pending→done 两跳
  expect(labels.length, `进度只出现了 ${labels.length} 个值：${labels}`).toBeGreaterThan(2);
  expect(labels.some((l) => l.includes('抽取音轨')), `没看到抽取音轨阶段：${labels}`).toBe(true);
  expect(labels.some((l) => l.includes('切分语音')), `没看到切分语音阶段：${labels}`).toBe(true);
  expect(labels[labels.length - 1]).toContain('done');

  const jobId = (await (await page.request.get(`${serverUrl}/api/jobs`)).json()).jobs[0].id;

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

    const src = await probe(FIXTURE);
    const dst = await probe(out);
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

  // 页面上有内嵌播放器（12 号票：成片能直接在页面里播放，不是只给外链）
  const video = page.locator('#jobs video').first();
  await expect(video).toBeVisible();
  await expect(video).toHaveAttribute('controls', '');

  // 播放器真的能播：等它拿到时长。preload="metadata" 只拉文件头，
  // 所以 readyState >= 1（HAVE_METADATA）就说明这一条通了。
  await expect(async () => {
    const st = await video.evaluate((v: HTMLVideoElement) => ({
      readyState: v.readyState,
      duration: v.duration,
      w: v.videoWidth,
      h: v.videoHeight,
      err: v.error?.message || '',
    }));
    expect(st.err, `播放器报错：${st.err}`).toBe('');
    expect(st.readyState, '播放器没拿到元数据').toBeGreaterThanOrEqual(1);
    expect(st.duration, '播放器拿不到时长').toBeGreaterThan(1);
    expect(st.w, '播放器拿不到画面尺寸').toBeGreaterThan(0);
  }).toPass({ timeout: 60_000, intervals: [500] });

  // 下载链接也在
  await expect(page.locator('#jobs a[href*="/video"][download]').first()).toBeVisible();
  await expect(page.locator('#jobs a[href*="/srt"][download]').first()).toBeVisible();
});

test('播放中轮询不会打断播放器', async ({ page, serverUrl }) => {
  // poll() 每 1.2 秒重建 #jobs 的 innerHTML，内嵌播放器之后这会把正在
  // 播的 <video> 换掉、播放从头开始。用户正看着成片校对字幕时被反复重置
  // 是不能接受的。
  await page.goto(serverUrl);
  await page.locator('#file').setInputFiles(FIXTURE);
  await expect(page.locator('#meta')).toContainText('时长');

  await expect(async () => {
    const jobs = (await (await page.request.get(`${serverUrl}/api/jobs`)).json()).jobs || [];
    expect(jobs[0]?.state).toBe('done');
  }).toPass({ timeout: 30 * 60 * 1000, intervals: [2000] });
  await page.request.post(`${serverUrl}/api/jobs/${(await (await page.request.get(`${serverUrl}/api/jobs`)).json()).jobs[0].id}/burn`);
  await expect(async () => {
    const jobs = (await (await page.request.get(`${serverUrl}/api/jobs`)).json()).jobs || [];
    expect(jobs[0]?.burn_state).toBe('done');
  }).toPass({ timeout: 30 * 60 * 1000, intervals: [2000] });

  const video = page.locator('#jobs video').first();
  await expect(video).toBeVisible();
  // 播到 2 秒，然后等好几轮轮询过去
  await video.evaluate((v: HTMLVideoElement) => { v.currentTime = 2; void v.play(); });
  await page.waitForTimeout(5000);
  const t = await video.evaluate((v: HTMLVideoElement) => v.currentTime);
  expect(t, '播放被轮询重置了').toBeGreaterThan(2);
});