import { test, expect, fixtureFor, shorterFixtureFor, runTool } from './fixture';
import { existsSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import path from 'node:path';

/**
 * 13 号票：E2E —— 字幕样式（双语/纯中文）与多作业并发隔离
 *
 * 真模型链路，跑法同 11 号票。
 */
test.use({ withModels: true });

const FIXTURE = fixtureFor('fixture.mp4');
const CLIP = shorterFixtureFor('fixture-40s.mp4');

// 真模型下一条视频的完整链路要几十秒到几分钟，容易撞上默认 5 分钟上限
test.setTimeout(45 * 60 * 1000);

/** 等作业全部到终态，返回它们。failed 非空即视为不通过。 */
async function waitAllDone(page: any, serverUrl: string, want: number) {
  await expect(async () => {
    const jobs = (await (await page.request.get(`${serverUrl}/api/jobs`)).json()).jobs || [];
    const failed = jobs.filter((j: any) => j.state === 'failed');
    expect(failed.map((j: any) => j.error), '有作业失败了').toEqual([]);
    expect(jobs.filter((j: any) => j.state === 'done').length, '还没做完')
      .toBeGreaterThanOrEqual(want);
  }).toPass({ timeout: 40 * 60 * 1000, intervals: [5000] });
  return (await (await page.request.get(`${serverUrl}/api/jobs`)).json()).jobs;
}

/** 等某个作业的压制完成，返回**刷新后**的作业对象。
 *
 * **POST /burn 之后不能立刻取成片** —— 那时 burn_state 还是 running，
 * `/api/jobs/{id}/video` 会回 409。
 *
 * 返回刷新后的对象很重要：`runOne` 拿到的快照里 `video_mp4` 还是空串
 * （压制还没发生），直接用它比较会得到"两次产物都是空"的假失败。
 */
async function waitBurnDone(page: any, serverUrl: string, jobId: string) {
  let fresh: any = null;
  await expect(async () => {
    const j = await (await page.request.get(`${serverUrl}/api/jobs/${jobId}`)).json();
    expect(j.burn_state, j.error || '压制还没完成').toBe('done');
    fresh = j;
  }).toPass({ timeout: 30 * 60 * 1000, intervals: [5000] });
  return fresh;
}

/** 等**指定文件名**的作业跑完，返回它。
 *
 * 与 `waitAllDone` 的区别：那个会断言"没有任何失败的作业"，适合"全部作业
 * 都该成功"的用例；本文件里有一个用例**故意**先交一个会被拒的文件，再用
 * 那个助手就会永远不满足条件、重试到超时。所以这里只盯目标作业。
 */
async function waitJobDone(page: any, serverUrl: string, filename: string) {
  let job: any = null;
  await expect(async () => {
    const jobs = (await (await page.request.get(`${serverUrl}/api/jobs`)).json()).jobs || [];
    const mine = jobs.find((j: any) => j.filename === filename);
    expect(mine, `没找到 ${filename} 的作业`).toBeTruthy();
    expect(mine.state, mine.error || '还没跑完').toBe('done');
    job = mine;
  }).toPass({ timeout: 30 * 60 * 1000, intervals: [3000] });
  return job;
}

test('两个不同视频各自产出自己的字幕，不串内容，串行不并发', async ({ page, serverUrl }) => {
  test.skip(!existsSync(CLIP), `缺少第二份素材：${CLIP}（跑 npx tsx e2e/make-fixtures.ts）`);
  expect(CLIP).not.toBe(FIXTURE);

  await page.goto(serverUrl);
  await expect(page.locator('#drop')).toBeVisible();

  // **必须用两个不同的视频**：提交同一个文件验不出串内容 —— 两份产物
  // 本来就该一模一样，串了也看不出来。
  await page.locator('#file').setInputFiles(FIXTURE);
  await page.locator('#file').setInputFiles(CLIP);

  // 串行：同一时刻最多一个在跑，其余排队
  await expect(async () => {
    const jobs = (await (await page.request.get(`${serverUrl}/api/jobs`)).json()).jobs || [];
    expect(jobs.length).toBeGreaterThanOrEqual(2);
    const running = jobs.filter((j: any) => j.state === 'running');
    expect(running.length, '两个作业同时在跑，串行失效').toBeLessThanOrEqual(1);
  }).toPass({ timeout: 5 * 60 * 1000, intervals: [3000] });

  const jobs = await waitAllDone(page, serverUrl, 2);

  const byName = (suffix: string) =>
    jobs.find((j: any) => j.filename === path.basename(suffix));
  const full = byName(FIXTURE);
  const part = byName(CLIP);
  expect(full, `没找到 ${path.basename(FIXTURE)} 的作业`).toBeTruthy();
  expect(part, `没找到 ${path.basename(CLIP)} 的作业`).toBeTruthy();

  const srtOf = async (j: any) =>
    (await page.request.get(`${serverUrl}/api/jobs/${j.id}/srt`)).text();
  const [a, b] = [await srtOf(full), await srtOf(part)];

  expect(a.length).toBeGreaterThan(0);
  expect(b.length).toBeGreaterThan(0);
  // 短 clip 只含前一段语音，字幕必然更少 —— 长度相同就说明串了内容
  const cuesOf = (s: string) => (s.match(/-->/g) || []).length;
  expect(cuesOf(b), '两个作业字幕条数一样，疑似串了内容').toBeLessThan(cuesOf(a));

  // 产物落在各自的目录里
  expect(full.srt).not.toBe(part.srt);
  expect(full.work_dir).not.toBe(part.work_dir);
});

/** 当前常驻推理服务的 pid，形如 ["asr:1234", "mt:5678"]（排序后便于比较）。 */
async function servicePids(page: any, serverUrl: string): Promise<string[]> {
  const j = await (await page.request.get(`${serverUrl}/api/runtime`)).json();
  return ((j.services || []) as { key: string; pid: number | null }[])
    .filter((s) => s.pid)
    .map((s) => `${s.key}:${s.pid}`)
    .sort();
}

/** 等到识别与翻译两个服务都就绪。 */
async function waitServicesReady(page: any, serverUrl: string) {
  await expect(async () => {
    const j = await (await page.request.get(`${serverUrl}/api/runtime`)).json();
    const ready = ((j.services || []) as { state: string }[])
      .filter((s) => s.state === 'ready');
    expect(ready.length, '推理服务还没就绪').toBe(2);
  }).toPass({ timeout: 5 * 60 * 1000, intervals: [2000] });
}

test('两个作业复用同一批常驻服务，不重复加载模型', async ({ page, serverUrl }) => {
  // 08 号票：复用是本项目的关键设计 —— 两个模型串行加载要 11 秒、常驻占
  // 2.6 GB。每个作业重新加载的话，用户每交一条视频都要多等十几秒，
  // 而且加载期间两个进程会抢 mlock。
  //
  // 证据必须是 **pid 不变**：进程被重启过的话 pid 必然变。只断言"服务是
  // ready 的"抓不到重启 —— 重启之后它照样会 ready。
  await page.goto(serverUrl);
  await page.locator('#file').setInputFiles(FIXTURE);

  await waitServicesReady(page, serverUrl);
  const before = await servicePids(page, serverUrl);
  expect(before, '两个推理服务没都起来').toHaveLength(2);

  await waitAllDone(page, serverUrl, 1);

  // 第二个作业。**必须先重新加载页面再选文件**：在同一个页面上对同一个
  // input 再 setInputFiles 同一份文件时，Chromium 不会重新触发 change
  // （文件列表没变），`upload()` 根本不会被调用 —— 作业建不出来，用例
  // 会一直等到超时。这一点很隐蔽：第一次选文件是好的，所以看起来像
  // "服务复用失败"，实际是测试自己没把第二个作业提交出去。
  await page.goto(serverUrl);
  await page.locator('#file').setInputFiles(FIXTURE);
  await waitAllDone(page, serverUrl, 2);

  const after = await servicePids(page, serverUrl);
  expect(after, '跑第二个作业时推理服务被重启了（pid 变了）').toEqual(before);
});

/** 跑一个**新**作业，返回它的 job 对象。
 *
 * 必须按"新出现的 id"找作业，**不能按 filename**：同一个用例里跑两次同一份
 * 素材时文件名相同，按它找会两次拿到同一个作业 —— 第二次的压制与断言全都
 * 作用在第一个作业上。症状很误导：表现为 409（压制还没开始就取成片）、
 * 或者"两次产物相同"，看起来像服务复用/隔离坏了，其实是测试选错了对象。
 */
async function runOne(page: any, serverUrl: string) {
  const before = new Set(
    ((await (await page.request.get(`${serverUrl}/api/jobs`)).json()).jobs || [])
      .map((j: any) => j.id));

  await page.goto(serverUrl);
  await page.locator('#file').setInputFiles(FIXTURE);
  await expect(page.locator('#meta')).toContainText('时长');

  const jobs = await waitAllDone(page, serverUrl, before.size + 1);
  const mine = jobs.find((j: any) => !before.has(j.id));
  expect(mine, '新作业没找到').toBeTruthy();
  return mine;
}

test('双语样式：中英成对，中文在上英文在下', async ({ page, serverUrl }) => {
  const CJK = /[一-鿿]/;
  const job = await runOne(page, serverUrl);
  const srt = await (await page.request.get(`${serverUrl}/api/jobs/${job.id}/srt`)).text();

  let checked = 0;
  for (const b of srt.trim().split(/\n\s*\n/)) {
    const lines = b.trim().split('\n').slice(2).filter(Boolean);
    if (!lines.length) continue;
    checked++;
    expect(CJK.test(lines[0]), `双语 cue 第一行不是中文：${b}`).toBe(true);
    if (lines.length > 1) {
      expect(CJK.test(lines[1]), `双语 cue 第二行不是英文：${b}`).toBe(false);
    }
  }
  expect(checked, '一条 cue 都没验到').toBeGreaterThan(0);
});

test('样式选择器真的把 mono 传到压制请求上', async ({ page, serverUrl }) => {
  // 样式只作用于**压制**（out.srt 永远是双语，那是 ASR 的原始产物）。
  // 这里截获压制请求，确认页面选择器接到了参数上 —— 之前 burn() 从不
  // 发 mono，纯中文只能手搓 URL 才生效。
  const job = await runOne(page, serverUrl);

  const seen: URL[] = [];
  page.on('request', (r) => {
    if (r.url().includes('/burn')) seen.push(new URL(r.url()));
  });

  await page.selectOption('#style', 'mono');
  await page.locator(`#jobs button:has-text("压制成片")`).first().click();
  await expect.poll(() => seen.length, { timeout: 30_000 }).toBeGreaterThan(0);
  expect(seen[0].searchParams.get('mono'), 'mono 没传到压制请求').toBe('true');

  // 压制用的 SRT 确实只剩中文（burn 把 sub_zh.srt 写进暂存目录）
  await waitBurnDone(page, serverUrl, job.id);
});

/** ffprobe：音轨条数 + 时长。样式切换不能把声音弄丢，这条是硬断言。 */
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

/** 从服务把成片下下来落盘，返回临时目录（调用方负责删）。 */
async function fetchVideo(page: any, serverUrl: string, jobId: string) {
  const dir = mkdtempSync(path.join(tmpdir(), 'vidsub-iso-'));
  const out = path.join(dir, 'out.mp4');
  const r = await page.request.get(`${serverUrl}/api/jobs/${jobId}/video`);
  expect(r.status(), '成片取不到').toBe(200);
  writeFileSync(out, await r.body());
  return { dir, out };
}

test('同一视频两档样式各出一份成片，都带声音且互不覆盖', async ({ page, serverUrl }) => {
  // 两个独立作业（各自目录），分别压成双语和纯中文。
  const bi = await runOne(page, serverUrl);
  await page.request.post(`${serverUrl}/api/jobs/${bi.id}/burn`);
  const biJob = await waitBurnDone(page, serverUrl, bi.id);
  const biDir = await fetchVideo(page, serverUrl, bi.id);

  const mo = await runOne(page, serverUrl);
  await page.request.post(`${serverUrl}/api/jobs/${mo.id}/burn?mono=true`);
  const moJob = await waitBurnDone(page, serverUrl, mo.id);
  const moDir = await fetchVideo(page, serverUrl, mo.id);

  try {
    // 两档都含音频流 —— 样式切换走的是不同分支（srt_for_style 的
    // mono 参数），最坏情况是 mono 那条把音频流弄丢
    const biProbe = await probe(biDir.out);
    const moProbe = await probe(moDir.out);
    expect(biProbe.audioStreams, '双语成片没有音轨').toBeGreaterThan(0);
    expect(moProbe.audioStreams, '纯中文成片没有音轨（样式切换把声音弄丢了）')
      .toBeGreaterThan(0);
    // 时长也要对，别压出一段截断的
    expect(biProbe.duration).toBeGreaterThan(1);
    expect(moProbe.duration).toBeGreaterThan(1);

    // 互不覆盖：两次产物路径不同、内容不同（纯中文字幕更少）
    expect(biJob.video_mp4).not.toBe(moJob.video_mp4);
    expect(biJob.video_mp4, '双语成片路径为空').toBeTruthy();
    expect(moJob.video_mp4, '纯中文成片路径为空').toBeTruthy();
    const [biSrt, moSrt] = await Promise.all([
      (await page.request.get(`${serverUrl}/api/jobs/${bi.id}/srt`)).text(),
      (await page.request.get(`${serverUrl}/api/jobs/${mo.id}/srt`)).text(),
    ]);
    expect(biSrt.length).toBeGreaterThan(0);
    expect(moSrt.length).toBeGreaterThan(0);
    // 两个成片字节不该完全一样（样式不同，字幕烧进画面也不同）
    const [a, b] = [readFileSync(biDir.out), readFileSync(moDir.out)];
    expect(Buffer.compare(a, b), '两档成片字节相同，第二次压制覆盖了第一次')
      .not.toBe(0);
  } finally {
    rmSync(biDir.dir, { recursive: true, force: true });
    rmSync(moDir.dir, { recursive: true, force: true });
  }
});

test('一个作业失败不影响另一个，失败原因可读', async ({ page, serverUrl }) => {
  await page.goto(serverUrl);

  // 先交一个成不成的（没有音轨的文件会被拒），再交一个正常的
  const bad = path.resolve(__dirname, '..', '.e2e-cache', 'no-audio.mp4');
  if (!existsSync(bad)) {
    await runTool('ffmpeg', [
      '-v', 'error', '-y', '-f', 'lavfi',
      '-i', 'testsrc=duration=3:size=320x240:rate=10',
      '-c:v', 'libx264', '-pix_fmt', 'yuv420p', bad,
    ]);
  }

  const badRes = await page.request.post(`${serverUrl}/api/jobs`, {
    multipart: { file: { name: 'no-audio.mp4', mimeType: 'video/mp4', buffer: readFileSync(bad) } },
  });
  // 缺音轨在上传阶段就该被拒（422），而不是跑完流水线才失败
  expect(badRes.status(), '没有音轨的视频没被拒').toBe(422);
  const badBody = await badRes.json();
  // 断言"音频轨"而不是"音轨"：服务端消息是「文件没有音频轨」，
  // 音与轨之间隔着"频"，子串并不连续 —— 写成 '音轨' 会永远失败。
  expect(badBody.error, '拒绝原因不可读').toContain('音频轨');

  // 正常作业照常跑完 —— 前一个失败不能把它带崩
  await page.locator('#file').setInputFiles(FIXTURE);

  // ⚠️ **不能复用 waitAllDone**：那个助手会断言"没有任何失败的作业"，
  // 而本用例里被拒的那个作业本来就是 failed —— 断言永远不成立，toPass
  // 会一路重试到 40 分钟超时才报错，看起来像"卡死"而不是"测试写错了"。
  // 这里只盯目标文件名的作业。
  const ok = await waitJobDone(page, serverUrl, path.basename(FIXTURE));
  expect(ok.state).toBe('done');
  expect(ok.error, '正常作业被前一个失败污染了').toBe('');
  const srt = await (await page.request.get(`${serverUrl}/api/jobs/${ok.id}/srt`)).text();
  expect(srt.length, '正常作业没产出字幕').toBeGreaterThan(0);
});
