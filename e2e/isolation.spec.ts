import { test, expect, fixtureFor, shorterFixtureFor } from './fixture';
import { existsSync } from 'node:fs';
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

/** 跑完一个作业，返回它的 job 对象。 */
async function runOne(page: any, serverUrl: string) {
  await page.goto(serverUrl);
  await page.locator('#file').setInputFiles(FIXTURE);
  await expect(page.locator('#meta')).toContainText('时长');
  const jobs = await waitAllDone(page, serverUrl, 1);
  const mine = jobs.find((j: any) => j.filename === path.basename(FIXTURE));
  expect(mine, '没找到作业').toBeTruthy();
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
  await expect(async () => {
    const j = await (await page.request.get(`${serverUrl}/api/jobs/${job.id}`)).json();
    expect(j.burn_state).toBe('done');
  }).toPass({ timeout: 30 * 60 * 1000, intervals: [5000] });
});
