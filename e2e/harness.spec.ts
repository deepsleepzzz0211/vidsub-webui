import { test, expect } from '@playwright/test';
import { spawnSync } from 'node:child_process';
import { createHash } from 'node:crypto';
import { existsSync, readFileSync } from 'node:fs';
import path from 'node:path';

const ROOT = path.resolve(__dirname, '..');
const CACHE = process.env.VIDSUB_E2E_CACHE || path.join(ROOT, '.e2e-cache');
const FIXTURE = path.join(CACHE, 'fixture.mp4');
const SOURCE = path.join(CACHE, 'source.mp3');

/**
 * 骨架探针（10 号票）
 *
 * 验证"跑 E2E"这件事本身可靠：素材能就位、服务能在隔离目录起来、
 * 失败时留有线索。业务用例在 11/12/13 号票。
 *
 * 关键区分：
 *   - **网络不通** → 跳过并说明原因（CI 无网时不该让流水线变红）
 *   - **校验失败 / ffmpeg 缺失** → 硬失败
 * 把后者也当成跳过，等于让"素材已损坏"变成一片绿，是最危险的一种失败。
 */

function runFixtureScript(): { ok: boolean; out: string; err: string } {
  const r = spawnSync('python', ['tools/fixture.py'], {
    cwd: ROOT, encoding: 'utf8', timeout: 300_000,
  });
  return { ok: r.status === 0, out: r.stdout || '', err: r.stderr || '' };
}

/** 只把"连不上网络"当作可跳过 */
function isNetworkProblem(msg: string): boolean {
  return /urlopen error|URLError|ECONNREFUSED|ENOTFOUND|EAI_AGAIN|timed out|网络|Connection refused/i
    .test(msg) && !/sha256|校验|期望|实际/.test(msg);
}

test('素材：真实公开视频已就位', () => {
  if (!existsSync(FIXTURE)) {
    const { ok, out, err } = runFixtureScript();
    if (!ok) {
      if (isNetworkProblem(err + out)) {
        test.skip(true, `素材无法下载（网络不通），跳过。注意：本机需设 VIDSUB_FIXTURE_PROXY`);
        return;
      }
      // 校验失败、ffmpeg 缺失等属于环境故障，必须暴露而不是藏起来
      throw new Error(`素材准备失败（不是网络问题，属环境故障）：\n${out}\n${err}`);
    }
  }
  expect(existsSync(FIXTURE), '素材文件不存在').toBe(true);
});

test('素材：源文件 sha256 与锁定值一致', () => {
  if (!existsSync(SOURCE)) {
    test.skip(true, '源文件未缓存（素材已就绪说明此前已通过校验）');
    return;
  }
  // 从 fixture.py 里读出锁定值，避免两处硬编码互相漂移
  const py = readFileSync(path.join(ROOT, 'tools', 'fixture.py'), 'utf8');
  const m = py.match(/EXPECTED_SHA256\s*=\s*"([0-9A-Fa-f]{64})"/);
  expect(m, 'fixture.py 里应声明 EXPECTED_SHA256').toBeTruthy();
  const got = createHash('sha256').update(readFileSync(SOURCE)).digest('hex');
  expect(got.toUpperCase()).toBe((m as RegExpMatchArray)[1].toUpperCase());
});

test('素材：确实是视频、带音频轨、时长符合声明', () => {
  if (!existsSync(FIXTURE)) {
    test.skip(true, '素材尚未生成');
    return;
  }
  const py = readFileSync(path.join(ROOT, 'tools', 'fixture.py'), 'utf8');
  const expectSec = Number(
    (py.match(/TRIM_SECONDS\s*=\s*(\d+)/) as RegExpMatchArray)[1]);

  const probe = spawnSync('ffprobe', [
    '-v', 'error', '-show_entries', 'stream=codec_type,duration',
    '-show_entries', 'format=duration', '-of', 'json', FIXTURE,
  ], { encoding: 'utf8', timeout: 30_000 });
  if (probe.status !== 0) {
    throw new Error(`ffprobe 不可用或素材损坏：${probe.stderr}`);
  }
  const info = JSON.parse(probe.stdout);
  const streams = info.streams || [];
  expect(streams.map((s: any) => s.codec_type)).toContain('video');
  expect(streams.map((s: any) => s.codec_type)).toContain('audio');

  // 静默失败防护：ffmpeg 在音频映射出错时会输出**无声**视频，
  // 只断言"有 audio 流"会漏掉它，所以还要验时长与声明一致。
  const dur = Number(info.format.duration);
  expect(
    Math.abs(dur - expectSec),
    `成片时长 ${dur}s 与声明的 ${expectSec}s 不符`,
  ).toBeLessThan(2.0);

  const audio = streams.find((s: any) => s.codec_type === 'audio');
  expect(Math.abs(Number(audio.duration) - expectSec)).toBeLessThan(2.0);
});
