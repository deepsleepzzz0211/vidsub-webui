# vidsub

上传视频，得到烧录好**中英双语字幕**的成片。全程本地运行，不联网调用任何 API。

> **状态：骨架阶段。** 服务能启动、浏览器能打开，上传与字幕功能在后续版本接入。

---

## ⚠️ 使用前必读：模型许可

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
   首次运行时下载。

完整条款见 [`THIRD_PARTY_NOTICES.md`](THIRD_PARTY_NOTICES.md) 与
[`MODEL_LICENSE-R2T2.md`](MODEL_LICENSE-R2T2.md)（协议全文逐字副本）。

**本节不是法律意见。** 商业分发或大规模部署前请自行确认授权状态。

## 运行

需要 Python 3.10+。

```bash
pip install -e .
python -m vidsub
```

浏览器会自动打开 `http://127.0.0.1:<端口>/`。端口自动挑选空闲的；
指定端口用 `python -m vidsub --port 8848`。

**重复执行不会启动第二个实例** —— 探测到已有实例时会直接在浏览器里打开它就退出。
两份模型各占几 GB 内存，同时跑会互相拖慢。

只监听 `127.0.0.1`，不对局域网暴露。

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

## 开发

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

首次运行会自动获取测试素材（源文件 3.3 MB，裁剪后 1.3 MB），缓存在
`.e2e-cache/`。**网络不通时会跳过并说明原因**；但**素材校验失败或 ffmpeg
缺失属于环境故障，会让测试失败** —— 把损坏的素材当成"跳过"会变成一片绿，
是最危险的一种失败。

首次在新机器上跑需要 `npx playwright install chromium`（本机已装）。

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
构建时会强制校验语音占比（当前 37.5%，下限 30%），可用
`python tools/check_fixture.py <文件>` 手动复验。

## 模型权重

第一次打开页面会看到权重清单（四个文件，约 2.6 GB）。它会**先查本机已有的
副本**，找不到才下载：

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
| `VIDSUB_DATA_DIR` | 权重落盘目录，默认 `~/.vidsub` |

直连遇到网络层错误会**自动回退代理一次**，并记住该域名，不再重复卡超时；
HTTP 4xx/5xx 不回退（404 是路径写错、416 是范围不合法，换出口也一样）。

`VIDSUB_DATA_DIR` 建议放在权重所在**同一个卷**上，这样认领走硬链接而不是
复制 2.6 GB。落盘路径不能含空格 —— 含空格的绝对路径会让 llama-server 拒绝启动。

## 许可

代码 MIT。模型权重许可见上方。
