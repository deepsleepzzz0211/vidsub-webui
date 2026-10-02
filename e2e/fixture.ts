import { test as base, expect } from '@playwright/test';
import { spawn, type ChildProcess } from 'node:child_process';
import { mkdtempSync, mkdirSync, rmSync, symlinkSync, existsSync } from 'node:fs';
import { createServer } from 'node:net';
import { tmpdir } from 'node:os';
import path from 'node:path';

const ROOT = path.resolve(__dirname, '..');
const PROBE = '/__vidsub_alive';

export type Fixtures = {
  serverUrl: string;
  dataDir: string;
  tmpDir: string;
  /** 数据目录里要不要挂真权重。默认 false —— "缺模型"那批用例依赖它。 */
  withModels: boolean;
};

/** 真模型链路用例：test.use({ withModels: true }) 单独打开。 */



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
 * 要上传的素材。
 *
 * 默认 150 秒的 fixture 跑真模型要十几分钟（226 段逐段识别+翻译），
 * 迭代时等不起。VIDSUB_E2E_FIXTURE 可以换成短片段（20 秒 ≈ 9 段），
 * 断言强度不变，只是覆盖面小一些 —— 时间轴/双语/音轨这些不变量与
 * 素材长度无关。
 */
export function fixtureFor(name: string): string {
  return process.env.VIDSUB_E2E_FIXTURE || path.resolve(ROOT, '.e2e-cache', name);
}

/**
 * 第二个素材（比第一个更短），用于"两个作业不串内容"。
 *
 * **必须真的比第一个短**：13 号票靠"两份字幕条数不同"来证明没串内容。
 * 如果两段素材等长，两份产物本来就该一样，串了也验不出来。
 */
export function shorterFixtureFor(name: string): string {
  const override = process.env.VIDSUB_E2E_FIXTURE_SHORTER;
  if (override) return override;
  // 不从 VIDSUB_E2E_FIXTURE 推导：那是"主素材"的覆盖值，拿它推出来的
  // clip 只会和主素材指向同一个文件，"两段不同内容"的前提就塌了。
  // 默认规则：去掉 -NNs 后缀（fixture-40s.mp4 -> fixture.mp4）
  const base = path.resolve(ROOT, '.e2e-cache', name);
  const m = /^(.*)-(\d+)s(\.[^.]+)$/.exec(path.basename(base));
  return m ? path.resolve(ROOT, '.e2e-cache', `${m[1]}${m[3]}`) : base;
}

/**
 * 每个用例一套隔离环境：
 * - 数据目录指向临时位置，**绝不读写开发者自己的 ~/.vidsub**
 * - HuggingFace / ModelScope 的缓存根也重定向到临时位置：否则"重扫缓存"
 *   会读到开发者自己 ~/.cache/huggingface 里的真实权重，导致结果随机器而变
 *   （别人机器上缓存里有 → 用例失败）
 * - 随机空闲端口，避免与本机在跑的实例冲突
 * - 只绑 127.0.0.1
 */
export const test = base.extend<Fixtures>({
  // 默认不挂真权重："缺模型时重定向到下载页"那批用例依赖这个前提。
  // 真模型用例自己写 test.use({ withModels: true })。
  withModels: [false, { option: true }],

  dataDir: async ({ withModels }, use) => {
    const dir = mkdtempSync(path.join(tmpdir(), 'vidsub-e2e-'));
    try {
      // 真模型 E2E：**只挂权重，不共享整个数据目录**。
      //
      // 曾经直接把 VIDSUB_DATA_DIR 透传给真实权重目录，结果每个用例的新
      // 服务进程都共享同一个 jobs/ —— 于是 "等 N 个作业 done" 数到的是
      // 历史作业，断言拿着陈旧的 job id 全盘失真。更隐蔽的是 pending：
      // 上一轮跑剩的作业标签停在 create() 的初始值「等待开始」，
      // 和 start() 写的「排队中」不同，一眼就能看出是残留。
      //
      // 权重是只读的，junction 挂进去既省 2.6 GB 复制又能被认领逻辑
      // 正常看到；jobs 目录留在临时目录里，每个用例真正独立。
      //
      // `withModels: false` 用来跑"缺模型"那批用例 —— 全局挂上真权重
      // 会让它们的前提（模型没就绪）整个消失。
      if (withModels) {
        const realModels = process.env.VIDSUB_REAL_MODELS_DIR;
        if (!realModels || !existsSync(realModels)) {
          throw new Error(
            '用例要求真权重，但没设 VIDSUB_REAL_MODELS_DIR。\n' +
            '  例：$env:VIDSUB_REAL_MODELS_DIR="D:\\vsdata\\models"',
          );
        }
        symlinkSync(realModels, path.join(dir, 'models'), 'junction');
      }
      await use(dir);
    } finally {
      // 临时目录要清理：每次跑留一个的话会单调堆积
      try { rmSync(dir, { recursive: true, force: true }); } catch { /* 尽力而为 */ }
    }
  },

  // 用户自己放权重的目录（用于"手动指定目录"这类用例）
  tmpDir: async ({ dataDir }, use) => {
    const dir = path.join(dataDir, 'user-library');
    mkdirSync(dir, { recursive: true });
    await use(dir);
  },

  serverUrl: async ({ dataDir }, use) => {
    const port = await freePort();
    const proc: ChildProcess = spawn(
      'python', ['-m', 'vidsub', '--port', String(port)],
      {
        cwd: ROOT,
        env: {
          ...process.env,
          // 每个用例独立的临时目录（真权重以 junction 挂进去）
          VIDSUB_DATA_DIR: dataDir,
          // llama-server 二进制：沿用调用方指定的
          ...(process.env.VIDSUB_LLAMA_SERVER
            ? { VIDSUB_LLAMA_SERVER: process.env.VIDSUB_LLAMA_SERVER } : {}),
          // 缓存根必须留在临时目录内：否则 /api/models 会去读开发者
          // 真实 ~/.cache/huggingface，结果随机器而变（别人机器上
          // 缓存里有 → 用例失败）
          HF_HOME: path.join(dataDir, 'hf'),
          HF_HUB_CACHE: path.join(dataDir, 'hf', 'hub'),
          MODELSCOPE_CACHE: path.join(dataDir, 'mscache'),
          // 免得本地请求被系统代理劫持
          http_proxy: '', https_proxy: '', HTTP_PROXY: '', HTTPS_PROXY: '',
          // 测试不该依赖开发机的代理设置：走哪条路由会改变
          // "重扫缓存"之类的结果
          VIDSUB_DOWNLOAD_PROXY: '',
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
