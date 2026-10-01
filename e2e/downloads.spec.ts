import { test, expect } from './fixture';
import path from 'node:path';

/**
 * 模型发现与认领（02 号票补充）
 *
 * 三条用户可见路径：
 * 1. 缺模型时首页把人送到下载页，页面上能看到四个权重与体积
 * 2. 页面说明"找过哪些地方"，并给出去 ModelScope 下载的可续传命令
 * 3. 用户自己指一个目录，按 sha256 认领里面已有的权重（不重新下载）
 *
 * 隔离数据目录里没有权重，所以这里验证的是接口契约与界面行为；
 * 真实下载行为由 tests/test_downloader.py 用本地 HTTP 服务覆盖。
 */

test('缺模型时首页重定向到下载页', async ({ page, serverUrl }) => {
  const resp = await page.goto(serverUrl, { waitUntil: 'domcontentloaded' });
  expect(resp?.status()).toBe(200);
  await expect(page).toHaveURL(/\/downloads$/);
});

test('下载页列出四个权重及体积', async ({ page, serverUrl }) => {
  await page.goto(`${serverUrl}/downloads`);
  await expect(page.locator('h1')).toContainText('模型权重');
  const items = page.locator('#items li');
  await expect(items).toHaveCount(4);
  await expect(items.first().locator('.name')).not.toBeEmpty();
  await expect(items.first().locator('.size')).toContainText(/B|KB|MB|GB/);
});

test('列出识别模型、音频侧投影、翻译模型、VAD 四类', async ({ page, serverUrl }) => {
  await page.goto(`${serverUrl}/downloads`);
  const body = page.locator('body');
  await expect(body).toContainText('语音识别主模型');
  await expect(body).toContainText('音频侧投影');
  await expect(body).toContainText('翻译模型');
  await expect(body).toContainText('语音活动检测');
});

test('显示 R2T2 的许可提醒', async ({ page, serverUrl }) => {
  await page.goto(`${serverUrl}/downloads`);
  await expect(page.locator('body')).toContainText('网易自定义协议');
  await expect(page.locator('body')).toContainText('不是 Apache 2.0');
});

test('页面说明找过哪些缓存目录', async ({ page, serverUrl, dataDir }) => {
  await page.goto(`${serverUrl}/downloads`);
  await expect(page.locator('body')).toContainText('找过这些地方');
  // 夹具把缓存根重定向到临时目录，页面要如实显示生效的路径，
  // 而不是写死的默认位置 —— 显示错路径会让人去改一个没生效的环境变量。
  const body = page.locator('body');
  await expect(body).toContainText(path.join(dataDir, 'hf'));
  await expect(body).toContainText(path.join(dataDir, 'mscache'));
  // 夹具同时设了 HF_HOME 与 HF_HUB_CACHE，而后者优先级更高，
  // 页面要报出**真正生效**的那个 —— 报错的话用户会去改一个没用的变量
  await expect(body).toContainText('由 HF_HUB_CACHE 指定');
  await expect(body).toContainText('由 MODELSCOPE_CACHE 指定');
});

test('找不到时给出可续传的单文件下载命令', async ({ page, serverUrl }) => {
  await page.goto(`${serverUrl}/downloads`);
  const curls = page.locator('.curl');
  await expect(curls).toHaveCount(4);
  // -C - 表示断了能接着下；且指向单个文件而非整个仓库
  await expect(curls.first()).toContainText('-C -');
  await expect(curls.first()).toContainText('.gguf');
});

test('提供手动指定目录的入口', async ({ page, serverUrl }) => {
  await page.goto(`${serverUrl}/downloads`);
  await expect(page.locator('#manual')).toBeVisible();
  await expect(page.locator('#adopt')).toBeVisible();
  await expect(page.locator('body')).toContainText('按 sha256 认领');
});

test('手动目录不存在时如实报错', async ({ page, serverUrl }) => {
  await page.goto(`${serverUrl}/downloads`);
  await page.locator('#manual').fill('C:\\definitely\\not\\here');
  await page.locator('#adopt').click();
  await expect(page.locator('#manualMsg')).toContainText('不存在');
});

test('目录里没有权重时说明是校验没过', async ({ page, serverUrl, tmpDir }) => {
  await page.goto(`${serverUrl}/downloads`);
  await page.locator('#manual').fill(tmpDir);
  await page.locator('#adopt').click();
  await expect(page.locator('#manualMsg')).toContainText('sha256');
});

test('模型状态接口返回清单与就绪状态', async ({ request, serverUrl }) => {
  const r = await request.get(`${serverUrl}/api/models`);
  expect(r.status()).toBe(200);
  const body = await r.json();
  expect(body.ready).toBe(false);
  expect(body.items).toHaveLength(4);
  expect(body.total_bytes).toBeGreaterThan(2 * 1024 ** 3);
  expect(body.missing.length).toBe(4);
});

test('状态接口说明找过哪些缓存', async ({ request, serverUrl }) => {
  const body = await (await request.get(`${serverUrl}/api/models`)).json();
  const kinds = body.caches.roots.map((r: { kind: string }) => r.kind);
  expect(kinds.sort()).toEqual(['huggingface', 'modelscope']);
  for (const r of body.caches.roots) expect(r.path).toBeTruthy();
});

test('每个缺失权重都有魔搭指引', async ({ request, serverUrl }) => {
  const body = await (await request.get(`${serverUrl}/api/models`)).json();
  expect(body.hints).toHaveLength(4);
  for (const h of body.hints) {
    expect(h.url).toMatch(/^https:\/\//);
    expect(h.curl).toContain('-C -');
    expect(h.size_text).toMatch(/(KB|MB|GB)/);
  }
});

test('重扫缓存时如实报告没找到', async ({ request, serverUrl }) => {
  const r = await request.post(`${serverUrl}/api/models/rescan`);
  expect(r.status()).toBe(200);
  const body = await r.json();
  expect(body.ready).toBe(false);
  expect(body.results.length).toBe(4);
  expect(body.results.every((x: { ok: boolean }) => x.ok === false)).toBe(true);
});

test('体重正：超过 2 GB 才切 GB', async ({ request, serverUrl }) => {
  const body = await (await request.get(`${serverUrl}/api/models`)).json();
  const asr = body.hints.find((h: { key: string }) => h.key === 'asr_model');
  // 1056 MB 不该显示成 "1.0 GB" —— 那样看不出是 1056 还是 1024
  expect(asr.size_text).toMatch(/MB$/);
});

test('数据目录隔离：权重不会落在用户目录', async ({ dataDir }) => {
  // 隔离目录里此时不该有任何 .gguf
  const { existsSync, readdirSync } = await import('node:fs');
  const path = await import('node:path');
  const walk = (d: string): string[] => {
    if (!existsSync(d)) return [];
    return readdirSync(d, { withFileTypes: true }).flatMap((e) =>
      e.isDirectory() ? walk(path.join(d, e.name)) : [path.join(d, e.name)]);
  };
  expect(walk(dataDir).filter((f) => f.endsWith('.gguf'))).toEqual([]);
});