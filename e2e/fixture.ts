import { test as base, expect } from '@playwright/test';
import { spawn, type ChildProcess } from 'node:child_process';
import { mkdtempSync, rmSync } from 'node:fs';
import { createServer } from 'node:net';
import { tmpdir } from 'node:os';
import path from 'node:path';

const ROOT = path.resolve(__dirname, '..');
const PROBE = '/__vidsub_alive';

export type Fixtures = {
  serverUrl: string;
  dataDir: string;
};

/**
 * 找一个空闲端口。
 *
 * 必须**异步**：`srv.listen(0)` 之后 `address()` 要等 listening 事件
 * 才有值，同步取会得到 null。之前写成同步取端口，导致所有依赖服务夹具的
 * 用例都报 "Cannot read properties of null (reading 'port')"。
 */
function freePort(): Promise<number> {
  return new Promise((resolve, reject) => {
    const srv = createServer();
    srv.once('error', reject);
    srv.listen(0, '127.0.0.1', () => {
      const addr = srv.address();
      const port = typeof addr === 'object' && addr ? addr.port : 0;
      srv.close(() => (port ? resolve(port) : reject(new Error('拿不到空闲端口'))));
    });
  });
}

async function waitUp(url: string, timeoutMs = 60_000, died?: () => boolean): Promise<boolean> {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    if (died?.()) return false;          // 进程已死就别再干等
    try {
      const r = await fetch(`${url}${PROBE}`, {
        signal: AbortSignal.timeout(2000),   // 单次请求也要超时，否则挂起会拖垮总时限
      });
      if (r.status === 200) return true;
    } catch {
      /* 还没起来 */
    }
    await new Promise((r) => setTimeout(r, 250));
  }
  return false;
}

/**
 * Windows 上 kill() 走 TerminateProcess：被杀的进程拿不到 atexit，
 * 服务自己登记的推理子进程不会被回收，会残留成孤儿（白占几 GB 内存）。
 * 所以要像 src/vidsub/server.py 里那样按 pid 连带子树一起杀。
 *
 * 注意 `proc.killed` 在**调用 kill 的瞬间**就变 true，不代表进程已退出，
 * 判断"是否还活着"必须看 exitCode / signalCode。
 */
async function stopTree(proc: ChildProcess): Promise<void> {
  const alive = () => proc.exitCode === null && proc.signalCode === null;
  if (!alive()) return;

  const killIt = () => {
    if (process.platform === 'win32' && proc.pid) {
      spawn('taskkill', ['/F', '/T', '/PID', String(proc.pid)], { stdio: 'ignore' });
    } else {
      proc.kill('SIGKILL');
    }
  };
  killIt();

  const deadline = Date.now() + 10_000;
  while (Date.now() < deadline && alive()) {
    await new Promise((r) => setTimeout(r, 100));
  }
  if (alive()) killIt();
}

/**
 * 每个用例一套隔离环境：
 * - 数据目录指向临时位置，**绝不读写开发者自己的 ~/.vidsub**
 * - 随机空闲端口，避免与本机在跑的实例冲突
 * - 只绑 127.0.0.1
 */
export const test = base.extend<Fixtures>({
  dataDir: async ({}, use) => {
    const dir = mkdtempSync(path.join(tmpdir(), 'vidsub-e2e-'));
    try {
      await use(dir);
    } finally {
      // 临时目录要清理：每次跑留一个的话会单调堆积
      try { rmSync(dir, { recursive: true, force: true }); } catch { /* 尽力而为 */ }
    }
  },

  serverUrl: async ({ dataDir }, use) => {
    const port = await freePort();
    const proc: ChildProcess = spawn(
      'python', ['-m', 'vidsub', '--port', String(port)],
      {
        cwd: ROOT,
        env: {
          ...process.env,
          VIDSUB_DATA_DIR: dataDir,
          // 免得本地请求被系统代理劫持
          http_proxy: '', https_proxy: '', HTTP_PROXY: '', HTTPS_PROXY: '',
          // 真实推理阶段会开浏览器窗口，E2E 里必须关掉
          VIDSUB_NO_BROWSER: '1',
        },
        stdio: ['ignore', 'ignore', 'pipe'],
      },
    );

    // 收集 stderr：起不来时要能说出原因，而不是只报"超时"
    let stderr = '';
    proc.stderr?.on('data', (d) => { stderr += String(d); });

    const url = `http://127.0.0.1:${port}`;
    if (!(await waitUp(url, 60_000, () => proc.exitCode !== null))) {
      await stopTree(proc);
      throw new Error(
        `服务在超时内没就绪（端口 ${port}）。\n` +
        `  退出码: ${proc.exitCode}\n` +
        `  stderr: ${stderr.slice(-800) || '(空)'}\n` +
        `  常见原因：fastapi/uvicorn 未安装，或端口被占。`,
      );
    }

    try {
      await use(url);
    } finally {
      await stopTree(proc);
    }
  },
});

export { expect };
