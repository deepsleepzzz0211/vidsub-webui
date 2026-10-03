# 贡献指南

本文档面向**要改这个项目的人**：开发环境、怎么跑测试、以及代码里那些看起来
奇怪的设计为什么必须那样。

只想用 vidsub 的话看 [README](README.md) 就够了。

---

## 目录

- [开发环境](#开发环境)
- [跑测试](#跑测试)
- [设计约束](#设计约束)
- [诊断工具](#诊断工具)
- [提 PR](#提-pr)

---

## 开发环境

```bash
pip install -e ".[dev]"
```

E2E 用 **Node 版 Playwright**（不是 Python 版），因为 Node 版能直接用系统已装的
Chromium，不必再拖一个浏览器内核。

```bash
npm install          # 装 @playwright/test（Playwright CLI 不含这个 runner 包）
npm run e2e          # 跑 E2E
npm run typecheck    # TypeScript 类型检查
```

首次在新机器上跑需要 `npx playwright install chromium`。

## 跑测试

单元测试（不起服务、不占端口）：

```bash
pytest -m "not e2e_support"
```

全量单测：

```bash
pytest
```

### 跑全量 E2E

**默认那几条需要真模型权重的用例会失败** —— 没有权重时它们本就无从验证。
要跑全量就显式给三个环境变量：

```powershell
$env:VIDSUB_REAL_MODELS_DIR="D:\vidsub\models"   # 换成你自己的，不要含空格
$env:VIDSUB_LLAMA_SERVER="D:\path\to\llama-server.exe"
$env:VIDSUB_E2E_FIXTURE=(Resolve-Path .e2e-cache\fixture-20s.mp4).Path
```

测试素材首次运行会自动获取（源文件 3.3 MB，裁剪后 1.3 MB），缓存在 `.e2e-cache/`。

**网络不通时会跳过并说明原因；但素材校验失败或 ffmpeg 缺失属于环境故障，会让
测试失败。** 这个区分是刻意的 —— 把损坏的素材当成"跳过"会变成一片绿，是最危险
的一种失败：看起来全过，其实什么都没验。

E2E 在**临时目录**里起隔离实例，不读写你自己的 `~/.vidsub`，跑完自动清理。
失败时保留 trace 与截图（`test-results/`），用 `npx playwright show-trace` 查看。

### 测试素材为什么是这一段

素材是 Wikimedia Commons 的一段公有领域朗读（Booker T. Washington 1895 年演说
片段），裁到 2.5 分钟并合成画面。选它是因为**单人独白、无背景音乐**。

这不是随便挑的：实测背景音乐（−13 dB）比人声（−26 dB）还响，会让语音活动检测
把音乐当成语音，切出来的段落全是错的。构建时会强制校验语音占比（当前 37.5%，
下限 30%），可用 `python tools/check_fixture.py <文件>` 手动复验。

## 设计约束

这几条都来自实际踩过的坑，写在代码里以免重犯。**改相关代码前请先读一遍** ——
每一条都对应一个曾经发生过的、症状很隐蔽的故障。

- **浏览器必须在 server 真的 listening 之后才打开。** 提前调用会让用户看到
  `ERR_CONNECTION_REFUSED`。
- **端口不能写死。** 被占用时要能自动换，并能给出可读提示。
- **退出时必须清理子进程。** Windows 上 detached 起的子进程不会随父进程退出，
  会残留成孤儿、白占几 GB 内存。
- **外部进程只接受不含空格的路径。** 含空格的绝对路径会让 llama-server 拒绝启动、
  让 ffmpeg 的字幕滤镜打不开文件。Windows 上项目目录带空格很常见，踩过。
- **静默失败必须配显式断言。** 例如音频流拷贝遇到不存在的流时 ffmpeg 不报错，
  会产出一个没有声音的成片。
- **发布"完成"之前必须把清理做完。** 否则客户端会在"已宣告完成"的窗口里读到没
  清干净的工作区 —— 症状是单独跑通过、全量跑失败的 flaky。
- **权重路径的"躲开空格"逻辑只能有一份。** 它在 `runtime.default_model_root()` 里。
  历史上复制过一次到服务侧，结果对 "John Smith" 这样的用户名每次都撞
  `SpaceInPathError`，而躲避逻辑一次都没执行过。

## 诊断工具

`tools/` 下都是**手动跑**的诊断脚本，不参与自动化测试。行为可疑时按症状挑一个：

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

## 提 PR

提交前请确保：

```bash
pytest                 # 单元测试全绿
npm run typecheck      # TS 类型检查
python -m pyflakes src/vidsub/*.py tools/*.py tests/*.py
```

**涉及推理行为的改动**（模型参数、提示词、切段逻辑）请**同时给出可复现性验证**：
同一输入跑两次，字幕要字节级一致。

这条不是洁癖 —— 它是"结果能不能被信任"的底线，而且违反它的症状很隐蔽：英文
转写层完全一致，**只有中文译文层不同**，所以只看英文或只看条数根本发现不了。
已知根因是 llama-server 的 prompt 缓存（默认开启），关温度、减线程、固定种子都
修不好它。`tools/check_determinism.py` 把这套对照实验固定下来了，改完跑一遍即可。
