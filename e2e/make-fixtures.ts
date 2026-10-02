/**
 * 造 E2E 素材：从 fixture.mp4 裁出几段不同时长的 clip。
 *
 * 为什么需要第二份**不同**的素材：13 号票要验"两个作业的产物各自正确、
 * 不串内容"。提交同一个文件两次验不出来 —— 两份产物本来就该一模一样，
 * 串了也看不出来。只有内容不同的两个输入，"产物不同"才能成为证据。
 *
 * 真模型链路下 150 秒素材要跑十几分钟，所以按需裁短：断言强度不变
 * （时间轴/双语/音轨这些不变量与素材长度无关），只是覆盖面小一些。
 *
 * 幂等：已存在就跳过（.e2e-cache 是缓存目录，不该每次跑都重新转码）。
 * 用 `-c copy` 不重编码，秒级完成。
 */
import { existsSync } from 'node:fs';
import path from 'node:path';

import { runTool } from './fixture';

const CACHE = path.resolve(__dirname, '..', '.e2e-cache');
const SRC = path.join(CACHE, 'fixture.mp4');

/** 要裁出的片段：[后缀, 秒数]。8s/12s 给并发隔离用例，20s 给单条链路。 */
const CLIPS: [string, number][] = [
  ['fixture-8s.mp4', 8],
  ['fixture-12s.mp4', 12],
  ['fixture-20s.mp4', 20],
  ['fixture-40s.mp4', 40],
];

if (!existsSync(SRC)) {
  console.error(`缺少源素材：${SRC}`);
  process.exit(2);
}

let made = 0;
for (const [name, seconds] of CLIPS) {
  const out = path.join(CACHE, name);
  if (existsSync(out)) continue;
  await runTool('ffmpeg', [
    '-v', 'error', '-y',
    '-i', SRC,
    '-t', String(seconds),
    '-c', 'copy',
    out,
  ]);
  console.log(`已生成：${name}（${seconds}s）`);
  made++;
}

console.log(made ? `共生成 ${made} 个片段` : '全部已存在，无需生成');
