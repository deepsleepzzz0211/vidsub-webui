# vidsub

本地运行的双语字幕工具：上传视频，得到烧录好中英双语字幕的成片

不用 GPU，不联网调用任何 API，不上传你的视频。识别、翻译、压制全部在本机
完成，模型只在你第一次打开页面时下载。

---

## 目录

- [⚠️ 使用前必读：模型许可](#使用前必读模型许可)
- [背景](#背景)
- [安装](#安装)
- [使用](#使用)
- [模型权重](#模型权重)
- [诊断工具](#诊断工具)
- [设计约束](#设计约束)
- [开发与测试](#开发与测试)
- [常见问题](#常见问题)
- [维护者](#维护者)
- [致谢](#致谢)
- [贡献](#贡献)
- [许可](#许可)

---

## 使用前必读：模型许可

**本项目源代码是 MIT 许可，但它依赖的识别模型不是开源许可。**

| 组件 | 许可 | 可否商用 |
|---|---|---|
| **Confucius4-R2T2**（识别） | **网易自定义模型使用协议** ⚠️ | **有条件**，见下 |
| Hy-MT2-1.8B（翻译） | Apache 2.0 | ✅ |
| Silero VAD v5.1.2 | MIT | ✅ |
| llama.cpp | MIT | ✅ |

识别模型使用的**不是** Apache 2.0，而是一份自定义协议。关键三条：

1. **商业授权门槛**（第 2.2 条）：月活超 1 亿或上年营收超人民币 10 亿，
   须向网易另行申请商业授权。个人自用与本地研究不受此限。
2. **禁止用于改进其他 AI 模型**（第 3.4c 条）：不允许用本项目的识别输出
   去训练或微调其他商业 AI 模型。
3. **须保留协议全文**（第 3.4b 条）：因此权重**不打包进 wheel、不进 Git LFS**，
   首次运行时下载，并随权重落盘一份协议副本。

完整条款见 [`THIRD_PARTY_NOTICES.md`](THIRD_PARTY_NOTICES.md) 与
[`MODEL_LICENSE-R2T2.md`](MODEL_LICENSE-R2T2.md)（协议全文逐字副本）。

**本节不是法律意见。** 商业分发或大规模部署前请自行确认授权状态。

## 背景

把一段视频做成双语字幕，现成的路数通常是：上传到某个云端服务，或者自己
拼 ffmpeg + Whisper + 某个翻译 API。前者要求你把视频交出去，后者要求你有
API key，且两者都要联网。

vidsub 想做的是第三种：**一个跑在自己机器上的网页**。拖一个视频进去，等它
跑完，拿到一份中文在上、英文在下的 SRT，以及一份字幕烧进画面的成片。没有
账号，没有额度，没有"这个文件会上传到我们的服务器"。

代价是它需要一个像样的 CPU 和大约 2.4 GB 磁盘（模型权重）。不需要显卡。

它的设计取舍几乎都来自一件事：**推理是本机 CPU 上最贵的操作**。所以模型
常驻不重复加载、作业排队串行执行、权重优先从本机已有副本里"认领"而不是
重新下载。这些决定散落在各模块的注释里，[设计约束](#设计约束) 一节摘了
最要紧的几条。

## 安装

需要 **Python 3.10+** 和 **ffmpeg**。

```bash
pip install -e .
```

### 依赖

| 依赖 | 用途 | 怎么装 |
|---|---|---|
| **Python 3.10+** | 运行时 | [python.org](https://www.python.org/downloads/) |
| **ffmpeg / ffprobe** | 抽音轨、探测媒体、烧录字幕 | Windows：`winget install Gyan.FFmpeg`；macOS：`brew install ffmpeg`；Linux：`apt install ffmpeg` |
| llama-server | 本地推理 | 不需要单独装 —— 首次运行时会下载对应平台的预编译包 |

ffmpeg 必须**在 PATH 里**。找不到时不会静默降级，而是给一条明确说"缺 ffmpeg"
的报错 —— 与其让你在传完视频、下完 2.4 GB 权重之后才失败，不如第一次就
说清楚。

自定义路径可用 `VIDSUB_FFMPEG` / `VIDSUB_FFPROBE` 指定。

## 使用

```bash
python -m vidsub
```

浏览器会自动打开 `http://127.0.0.1:<端口>/`，端口自动挑空闲的。

第一次打开会看到模型下载页（四个文件，约 2.4 GB），详见 [模型权重](#模型权重)。
权重齐了之后首页就是上传区：拖一个视频进去，页面上会显示时长、分辨率、体积，
提交后进入处理状态，跑完在页面里直接能看到中英对照的字幕。

### 命令行

```bash
python -m vidsub                 # 自动挑端口，自动开浏览器
python -m vidsub --port 8848     # 指定端口
```

**重复执行不会启动第二个实例** —— 探测到已有实例时会直接在浏览器里打开它
就退出。两份模型各占几 GB 内存，同时跑会互相拖慢。

只监听 `127.0.0.1`，不对局域网暴露。

### 字幕样式

页面上可以选两档，**压制成片时生效**：

| 样式 | 效果 |
|---|---|
| 中英双语（默认） | 中文在上、英文在下，底部半透明压条 |
| 纯中文 | 只留中文行 |

字幕条数多时会自动分段，避免一整条长句盖掉半个画面。成片压制完可以在页面
里直接播放，也能单独下载成片或 SRT。

### 产物落在哪

- **页面上下载**是最直接的方式（成片 / SRT 都有下载链接）。
- 磁盘上的位置是 `<VIDSUB_DATA_DIR>/jobs/<作业 id>/`，默认
  `VIDSUB_DATA_DIR` 是 `~/.vidsub`。
- **中间品（切段音频、临时目录）用完即清**，但**成片和 SRT 不会自动删除** ——
  它们是你的产物，什么时候清理由你决定。

### 处理要多久

本机 CPU 实测：**226 秒的视频约 1.5 分钟出字幕**（含首次加载模型），大致比
实时快一倍多。压制成片另算，通常比识别快。

第一次运行还要额外等模型下载（取决于网速）。之后模型常驻内存，连续处理多条
不会重复加载。

## 模型权重

第一次打开页面会看到权重清单（四个文件，合计 2,591,149,252 字节 ≈ 2.4 GB）。
它会**先查本机已有的副本**，找不到才下载：

| 来源 | 默认位置 | 目录形态 |
|---|---|---|
| HuggingFace | `~/.cache/huggingface/hub` | `models--<org>--<repo>/snapshots/<版本>/<文件>` |
| ModelScope 1.37 | `~/.cache/modelscope/hub` | `<org>/<repo>/<文件>` |
| ModelScope 旧版 | 同上 | `models--<org>--<repo>/...` |

认领按 **sha256 + 体积**判定，文件名和层级都不要求；同盘走硬链接，不占额外
空间。你也可以在下载页手动指定一个目录，把里面已有的权重认领进来。

都找不到时，页面给出每个权重在 ModelScope 上的**直链与可续传命令**
（`curl -L -C -`），只下单个文件 —— 不建议整个仓库，GGUF 仓库里还有
Q8_0 / f16 等其它量化，整个下会多花好几倍磁盘和时间。

### 网络相关的环境变量

默认**直连**下载。实测 ModelScope 直连约 21–40 MB/s，走代理反而只有
240 KB/s；但 GitHub 例外 —— VAD 模型在 GitHub 上，直连会在中途截断，
所以自动走代理。

| 变量 | 作用 |
|---|---|
| `VIDSUB_DOWNLOAD_PROXY` | 代理地址，如 `http://127.0.0.1:7897` |
| `VIDSUB_FORCE_DIRECT=0` | 全部走代理（墙内网络） |
| `VIDSUB_PROXY_HOSTS` | 追加需要走代理的域名，逗号分隔 |
| `VIDSUB_DOWNLOAD_TIMEOUT` | 单次请求超时秒数，默认 60 |
| `VIDSUB_DATA_DIR` | 权重与作业的落盘目录，默认 `~/.vidsub` |

直连遇到网络层错误会**自动回退代理一次**，并记住该域名，不再重复卡超时；
HTTP 4xx/5xx 不回退（404 是路径写错、416 是范围不合法，换出口也一样）。

`VIDSUB_DATA_DIR` 建议放在权重所在**同一个卷**上，这样认领走硬链接而不是
复制 2.4 GB。落盘路径不能含空格 —— 含空格的绝对路径会让 llama-server 拒绝启动。

### 其它环境变量

| 变量 | 作用 |
|---|---|
| `VIDSUB_NO_BROWSER=1` | 启动时不打开浏览器（E2E、远程终端用） |
| `VIDSUB_LLAMA_SERVER` | 自定义 llama-server 可执行文件路径 |
| `VIDSUB_IDLE_TIMEOUT` | 空闲多久回收常驻模型，默认 1800 秒；`0` 表示不回收 |
| `VIDSUB_SSE_MAX_SECONDS` | 进度推送最长时长，默认 43200 秒（12 小时） |
| `VIDSUB_VAD_MODEL` | 自定义 VAD 模型路径 |

## 诊断工具

`tools/` 下都是**手动跑**的诊断脚本，不参与自动化测试。模型行为可疑时按
症状挑一个：

| 症状 | 用哪个 |
|---|---|
| 同一视频两次结果不一致 | `python tools/check_determinism.py` —— 对照实验，定位是不是推理侧不确定 |
| 想知道权重到底找没找到、找过哪些缓存 | `python tools/probe_cache.py` |
| 手上有权重想确认能不能认领 | `python tools/verify_adopt.py [源目录] [数据目录]` |
| 怀疑推理服务起不来 / 参数有问题 | `python tools/verify_runtime.py` —— 真二进制真权重跑一遍 |
| 想知道字幕质量有没有退化 | `python tools/check_regression.py` —— 与质量基线比对 |
| 字幕质量本身要量化（字错率） | `python tools/check_quality.py` |
| 测试素材本身合不合格 | `python tools/check_fixture.py <文件>` |

多数脚本认 `VIDSUB_DATA_DIR`，默认指向项目的模型目录。

## 设计约束

这几条来自实际踩过的坑，写在代码里以免重犯：

- **浏览器必须在 server 真的 listening 之后才打开。** 提前调用会让用户看到
  `ERR_CONNECTION_REFUSED`。
- **端口不能写死。** 被占用时要能自动换，并能给出可读提示。
- **退出时必须清理子进程。** Windows 上 detached 起的子进程不会随父进程退出，
  会残留成孤儿、白占几 GB 内存。
- **外部进程只接受不含空格的路径。** 含空格的绝对路径会让 llama-server 拒绝
  启动、让 ffmpeg 的字幕滤镜打不开文件。
- **静默失败必须配显式断言。** 例如音频流拷贝遇到不存在的流时 ffmpeg 不报错，
  会产出一个没有声音的成片。
- **发布"完成"之前必须把清理做完。** 否则客户端会在"已宣告完成"的窗口里读到
  没清干净的工作区 —— 症状是单独跑通过、全量跑失败的 flaky。

## 开发与测试

```bash
pip install -e ".[dev]"
pytest
```

### 端到端测试

E2E 用 Node 版 Playwright（不是 Python 版），因为本机已有 Chromium 浏览器。

```bash
npm install          # 装 @playwright/test（CLI 不含这个 runner 包）
npm run e2e          # 跑 E2E
npm run typecheck    # TypeScript 类型检查
```

首次在新机器上跑需要 `npx playwright install chromium`。

**默认那几条需要真模型权重的用例会失败**（没有权重时它们本就无从验证）。
要跑全量就显式给三个环境变量：

```powershell
$env:VIDSUB_REAL_MODELS_DIR="D:\vidsub\models"   # 换成你自己的，不要含空格
$env:VIDSUB_LLAMA_SERVER="D:\path\to\llama-server.exe"
$env:VIDSUB_E2E_FIXTURE=(Resolve-Path .e2e-cache\fixture-20s.mp4).Path
```

测试素材首次运行会自动获取（源文件 3.3 MB，裁剪后 1.3 MB），缓存在
`.e2e-cache/`。**网络不通时会跳过并说明原因**；但**素材校验失败或 ffmpeg
缺失属于环境故障，会让测试失败** —— 把损坏的素材当成"跳过"会变成一片绿，
是最危险的一种失败。

本机访问 Wikimedia 需要代理：

```bash
VIDSUB_FIXTURE_PROXY=http://127.0.0.1:7897 npm run e2e
```

E2E 在**临时目录**里起隔离实例，不读写你自己的 `~/.vidsub`，跑完自动清理。
失败时保留 trace 与截图（`test-results/`），用 `npx playwright show-trace` 查看。

只跑单元测试（不起服务、不占端口）：

```bash
pytest -m "not e2e_support"
```

测试素材是一段公有领域朗读音频（Wikimedia Commons，Booker T. Washington 1895
年演说片段），裁到 2.5 分钟并合成画面。选它是因为**单人独白、无背景音乐**：
实测背景音乐（−13 dB）比人声（−26 dB）还响，会让语音活动检测把音乐当成语音。
构建时会强制校验语音占比（当前 37.5%，下限 30%）。

## 常见问题

**处理到一半失败了，怎么看原因？**
页面上那条作业会显示可读的失败原因（不带 traceback）。完整的服务端日志在
`<VIDSUB_DATA_DIR>` 下；推理服务自己的日志也在那里。

**能不能不重新下载，用我已经有的权重？**
能。下载页有"扫描本机缓存"按钮（去 HuggingFace / ModelScope 标准位置找），
也可以手动指一个目录让它按 sha256 认领。两种方式都不重新下载。

**模型能一直占着内存吗？**
默认空闲 30 分钟回收。想让它常驻就设 `VIDSUB_IDLE_TIMEOUT=0`。

**为什么不用 GPU？**
目标场景是"手边这台机器就能跑"，不假设有独显。CPU 上实测 226 秒素材约
1.5 分钟，够用。

**支持哪些格式？**
常见容器都行（mp4 / mkv / webm / mov 等）。**必须带音轨** —— 没有音轨的文件
在上传阶段就会被拒（422），不会让你等到跑完才发现。

**视频会被上传到哪儿吗？**
不会。服务只监听 `127.0.0.1`，除了首次下载模型权重之外没有任何外部请求。

## 维护者

单人维护。仓库目前尚未发布到公开托管平台，发布后本节会补上地址。

## 致谢

- [llama.cpp](https://github.com/ggml-org/llama.cpp) —— 本地推理的底座
- [Confucius4-R2T2](https://www.modelscope.cn/) —— 语音识别模型（网易）
- [Hy-MT2](https://www.modelscope.cn/) —— 翻译模型
- [Silero VAD](https://github.com/snakers4/silero-vad) —— 语音活动检测
- 测试素材来自 [Wikimedia Commons](https://commons.wikimedia.org/) 的公有领域录音

## 贡献

欢迎开 issue 讨论，也接受 PR。仓库发布后 Issues 会开在同一位置。

报 bug 时如果能附上：操作系统、Python 版本、视频的时长与编码、页面上显示的
失败原因 —— 会快很多。

提交 PR 前请确保：

```bash
pytest                 # 单元测试全绿
npm run typecheck      # TS 类型检查
python -m pyflakes src/vidsub/*.py tools/*.py tests/*.py
```

涉及推理行为的改动（模型参数、提示词、切段逻辑）请**同时给出可复现性验证**：
同一输入跑两次，字幕要字节级一致。这条不是洁癖 —— 它是"结果能不能被信任"
的底线，而且违反它的症状很隐蔽：英文转写层完全一致，只有中文译文层不同。

## 许可

源代码 [MIT](LICENSE)。

**模型权重不是 MIT** —— 识别模型使用网易自定义协议，商业使用有条件限制。
详见 [使用前必读](#使用前必读模型许可) 与
[`MODEL_LICENSE-R2T2.md`](MODEL_LICENSE-R2T2.md)。
