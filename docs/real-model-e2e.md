# 真模型 E2E 怎么跑

11/12/13 三张工单要在**真权重 + 真 llama-server** 上验证完整链路：
上传 → VAD 切段 → ASR → 翻译 → 双语 SRT → 压制成片 → 下载。

默认 `npx playwright test` 里这三条会失败（没有权重时它们本就无从验证），
所以要显式给三个环境变量。

## 一次性准备

```powershell
# 1. 权重目录（D:\vsdata\models 下需有 r2t2/ hy-mt2/ vad/ 四个文件）
$env:VIDSUB_REAL_MODELS_DIR="D:\vsdata\models"

# 2. llama-server 二进制（路径**可以含空格**，只有模型参数路径怕空格）
$env:VIDSUB_LLAMA_SERVER="D:\path\to\llama-server.exe"

# 3. 素材：短片段省时间。150 秒全跑要十几分钟，20 秒只要一分多钟。
#    断言强度不变 —— 时间轴/双语/音轨这些不变量与素材长度无关。
$env:VIDSUB_E2E_FIXTURE=(Resolve-Path .e2e-cache\fixture-20s.mp4).Path
$env:VIDSUB_E2E_FIXTURE_SHORTER=(Resolve-Path .e2e-cache\fixture-8s.mp4).Path

# 素材由 tools/make 脚本从 fixture.mp4 裁出（幂等）：
npx tsx e2e\make-fixtures.ts
```

## 跑

```powershell
$env:VIDSUB_IDLE_TIMEOUT="0"    # 别让空闲巡检在作业中途回收模型
npx playwright test --workers=1
```

`--workers=1` 是必须的：每条用例要起两个 llama-server（各 2.6 GB 权重），
并发跑会互相抢内存。

## 只想跑真模型那几条

```powershell
npx playwright test upload-to-srt burn isolation --workers=1
```

## 隔离是怎么做的

`VIDSUB_REAL_MODELS_DIR` **只挂权重，不共享整个数据目录**：

- 每个用例一个临时数据目录，`models` 用 junction 指到真权重（省 2.6 GB 复制）
- `jobs/` 留在临时目录里 —— 每个用例真正独立

早先的写法是把 `VIDSUB_DATA_DIR` 直接透传给真实权重目录，结果所有用例共享
同一个 `jobs/`，于是"等 N 个作业完成"数到的是历史作业，断言拿着陈旧的 job id
全盘失真。

真权重是**逐用例显式打开**的（`test.use({ withModels: true })`），默认关闭 ——
"缺模型时重定向到下载页"那批用例依赖模型未就绪这个前提。

## 退出码约定（`tools/check_regression.py`）

| 码 | 含义 |
|---|---|
| 0 | 跑过了，两次产出字节级一致 |
| 1 | 跑过了，但不一致（或与基线不符）—— **真问题** |
| 2 | 环境错误（视频不存在、服务起不来） |
| 3 | SKIP：权重不齐备。**不等于通过** |

CI 里务必区分 1 和 3：把 skip 当成通过是最坏的一种。

## 已知耗时（本机实测，20 秒素材）

| 环节 | 耗时 |
|---|---|
| 认领权重（sha256 校验 + 硬链接） | 2.6 s |
| 两个 llama-server 串行就绪 | 11.5 s |
| 复用（第二个作业） | 0.38 s，pid 不变 |
| 一条 20 秒视频出字幕（9 段） | 约 30 s |
| 压制成片 | 约 25 s |
