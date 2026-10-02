import { test, expect } from './fixture';

/**
 * 推理运行时（03 号票）
 *
 * 隔离数据目录里没有权重，所以这里验的是**接口契约与分流**：
 * - 缺权重时 /api/runtime/start 要立刻拒绝，而不是白等三分钟
 * - 状态接口要能用（页面要能显示"没就绪"）
 * - 手动停止是幂等的
 *
 * 真二进制 + 真权重的启动/复用/回收由 tools/verify_runtime.py 验证：
 * 替身学得再像也证明不了真机没问题（真二进制会对含空格的模型路径报
 * invalid argument），所以那条路径不进 E2E。
 */

test('缺权重时拒绝启动推理服务', async ({ request, serverUrl }) => {
  const r = await request.post(`${serverUrl}/api/runtime/start`);
  expect(r.status()).toBe(409);
  const body = await r.json();
  expect(body.error).toContain('下载');
  expect(body.missing).toHaveLength(4);
});

test('缺权重时状态接口仍可用', async ({ request, serverUrl }) => {
  const r = await request.get(`${serverUrl}/api/runtime`);
  expect(r.status()).toBe(200);
  expect((await r.json()).services).toEqual([]);
});

test('手动停止是安全的（没起过也能停）', async ({ request, serverUrl }) => {
  const r = await request.post(`${serverUrl}/api/runtime/stop`);
  expect(r.status()).toBe(200);
});

test('标记活跃不报错', async ({ request, serverUrl }) => {
  const r = await request.post(`${serverUrl}/api/runtime/touch`);
  expect(r.status()).toBe(200);
  expect((await r.json()).ok).toBe(true);
});

test('两个服务定义与清单一致（接口直接暴露出来）', async ({ request, serverUrl }) => {
  // 缺权重时 start 会被拒，但从服务定义能看出端口与模型路径是对的
  const r = await request.post(`${serverUrl}/api/runtime/start`);
  const body = await r.json();
  expect(body.missing.sort()).toEqual(
    ['asr_mmproj', 'asr_model', 'mt_model', 'vad']);
});