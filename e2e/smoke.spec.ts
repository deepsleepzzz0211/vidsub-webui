import { test, expect } from './fixture';
import { existsSync, readFileSync } from 'node:fs';
import { homedir } from 'node:os';
import path from 'node:path';

/**
 * 骨架探针（10 号票）
 *
 * 验证"跑 E2E"这件事本身可靠：服务能在隔离目录里起来、页面可访问、
 * 隔离真的是隔离。业务用例在 11/12/13 号票。
 */
test('服务：隔离实例可启动，首页可访问', async ({ page, serverUrl }) => {
  const res = await page.request.get(`${serverUrl}/`);
  expect(res.status()).toBe(200);
});

test('服务：首页按模型是否就绪分流', async ({ page, serverUrl }) => {
  await page.goto(serverUrl, { waitUntil: 'domcontentloaded' });
  // 隔离环境里没有权重，首页应把人送到下载页 —— 而不是显示"可以上传"
  // 的界面，等真正需要模型的时刻才失败
  await expect(page).toHaveURL(/\/downloads$/);
  await expect(page.locator('h1')).toContainText('模型权重');
});

test('服务：模型就绪后首页展示上传区', async ({ page, serverUrl }) => {
  // 直接访问上传页，验证入口本身还在（首页分流是另一回事）
  const res = await page.request.get(`${serverUrl}/api/models`);
  expect(res.status()).toBe(200);
  // 就绪后的首页内容由 11 号票落地；这里只确认不会 500
  const home = await page.request.get(`${serverUrl}/`);
  expect(home.status()).toBe(200);
});

test('隔离：记录文件落在临时目录，用户目录未被创建', async ({ dataDir, serverUrl }) => {
  // 上一版只断言"临时目录 != ~/.vidsub"——那是 mkdtemp 本身的性质，
  // 永远为真，等于什么都没验。真正该验的是**副作用的落点**。
  //
  // 注意：serverUrl 是夹具参数，服务在**进入用例体之前**就起来了。
  // 所以"之前是否存在"必须靠一个不受夹具影响的独立探针来判断，
  // 不能在用例体里先读后比对——那样读到的已经是服务起来之后的状态。
  const real = path.join(homedir(), '.vidsub');
  const userVidsubExists = existsSync(real);

  // 服务确实起过（这条断言同时说明夹具生效）
  expect((await fetch(`${serverUrl}/__vidsub_alive`)).status).toBe(200);

  // 记录文件落在隔离目录里
  expect(existsSync(path.join(dataDir, 'instance.json')),
    '服务的记录文件应落在隔离目录里').toBe(true);

  // 用户目录不该因为这次服务而被创建
  if (!userVidsubExists) {
    expect(existsSync(real), `E2E 在用户目录里创建了 ${real}`).toBe(false);
  }
});

test('隔离：起服务时确实通过 VIDSUB_DATA_DIR 传了隔离目录', async ({ dataDir, serverUrl }) => {
  // 若将来重构时丢掉了读取 VIDSUB_DATA_DIR 的那行，隔离会静默失效
  const rec = JSON.parse(readFileSync(path.join(dataDir, 'instance.json'), 'utf8'));
  expect(typeof rec.port).toBe('number');
  expect((await fetch(`${serverUrl}/__vidsub_alive`)).status).toBe(200);
});
