/**
 * 造第二份 E2E 素材：前 40 秒的 clip。
 *
 * 为什么必须有第二份**不同**的素材：13 号票要验的是"两个作业的产物各自
 * 正确、不串内容"。提交同一个文件两次验不出来 —— 两份产物本来就该一模一样，
 * 串了也看不出来。只有内容不同的两个输入，"产物不同"才能成为证据。
 *
 * 幂等：已经存在就跳过（.e2e-cache 是缓存目录，不该每次跑都重新转码）。
 */
import { execFileSync } from 'node:child_process';
import { existsSync } from 'node:fs';
import path from 'node:path';

const CACHE = path.resolve(__dirname, '..', '.e2e-cache');
const SRC = path.join(CACHE, 'fixture.mp4');
const CLIP = path.join(CACHE, 'fixture-40s.mp4');

if (!existsSync(SRC)) {
  console.error(`缺少源素材：${SRC}（先跑 tools 里的下载步骤）`);
  process.exit(2);
}
if (existsSync(CLIP)) {
  console.log(`已存在，跳过：${CLIP}`);
  process.exit(0);
}

execFileSync('ffmpeg', [
  '-v', 'error', '-y',
  '-i', SRC,
  '-t', '40',
  '-c', 'copy',
  CLIP,
], { stdio: 'inherit' });

console.log(`已生成：${CLIP}`);
