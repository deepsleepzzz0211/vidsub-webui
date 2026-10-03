# 死代码清理：判定标准与复核方法

一次性清理很容易变成"我看不懂所以删了"。这份文档把**判据**和**怎么验**
写下来，下次照着跑，不用重新发明。

---

## 一、判定标准

一个符号要删，必须满足下面**至少一条**，并留下证据：

| # | 判据 | 证据形式 |
|---|---|---|
| 1 | **零引用** | 除自身定义外，全仓搜不到引用 |
| 2 | **只写不读** | 赋了值，但没有任何地方读它 |
| 3 | **被取代** | 能力已由新接口提供，只剩自己的测试在调 |
| 4 | **不可达 / 恒真** | `if False`、`assert X or True`、`while 0` |

**搜索范围必须包括**：`src/`、`tools/`、`tests/`、`e2e/`、`docs/`、
`README.md`、`src/vidsub/web/*.html`（前端有 `onclick="burn(...)"`
这类按名字调的引用）。

---

## 二、明确**不删**的四类

这四类是最常见的假阳性，删了就是事故：

1. **框架回调**
   FastAPI 路由（按装饰器注册）、`http.server` 的 `do_GET`/`do_POST`/
   `log_message`、pytest 夹具（按参数名注入）。
   vulture 一律看不见它们 —— **这是它最大的噪声来源**。

2. **封闭词表的值**
   `registry.Source.HUGGINGFACE`、`models.STATE_PARTIAL` 之类。
   "当前没有比较"不等于"不可能出现"：`STATE_PARTIAL` 是
   `downloader.status_of()` 真的会返回的值，删了会让词表对调用方撒谎。

3. **可执行的不变量**
   `styles.thresholds_fit`、`registry.total_size_bytes`、
   `tools/fixture.EXPECTED_SECONDS`。
   唯一调用者就是守护它的那条测试 —— 那不是死代码，是不变量的可执行形式。
   删掉函数 = 不变量再也没法验。

4. **公开入口点的返回类型**
   比如 `models.Task` 的 `progress()` / `wait()`：`download()` 返回它，
   调用方要能消费结果。生产代码可能只用 `.id`，但那是接口存在的意义。

---

## 三、怎么跑

```bash
# 1) 机械类：未用导入 / 重定义 / 未用局部变量
python -m ruff check --select F401,F811,F841 --no-cache src/ tools/ tests/

# 2) 冗余分支 / 不可达（SIM222 抓恒真三元、B 系列抓可疑表达式）
python -m ruff check --select SIM,B018,B023,F502,F503,F504,F522,F523,F631,F632,F633,F634,F701,F702,F704,F706,F707,F811,F823,F841 --no-cache src/ tools/ tests/

# 3) 全程序可达性（vulture 装在隔离环境，不污染项目）
~/.workbuddy-ai/binaries/python/envs/default/Scripts/python.exe \
    -m vulture src/ tools/ tests/ --min-confidence 80
```

**⚠️ 第 3 步必须把 `tests/` 一起纳入扫描。** 只扫 `src/` 的话，
"只被测试用到"的符号会被误报成死代码 —— 第一轮漏了这一步，清单里一半是
假阳性。

---

## 四、逐条确认（不可跳过）

工具只给候选，**每条都要人工确认**：

```bash
grep -rn "\b符号名\b" src/ tools/ tests/ e2e/ docs/ README.md src/vidsub/web/
```

- 出现 1 次 = 只有定义 → 候选成立
- 出现 2 次 = 看清第二处是真引用还是文档提及
- **文件级判断要看导入图，不能看文件名**：`tools/e2e_server.py` 按文件名搜
  是 0 引用，实际被 `import e2e_server as es` 引用。

---

## 五、删完必须重跑

**删死代码会连锁暴露更多死代码。** 实例：删掉 `pipeline.fmt_ts` 之后，
顶层的 `styles` 导入才暴露为未使用，同时发现 `write_srt` 里还有个重复的
局部导入。

所以流程是：删 → 重跑静态检查 → 还有就再删 → 直到收敛。

最后四道门：

```bash
python -m pytest -q                    # 单测
npx playwright test --workers=1        # E2E（真机，需要权重）
python -m pyflakes src/vidsub/*.py tools/*.py tests/*.py
npx tsc --noEmit
```

---

## 六、最值得找的一类：恒真断言

比没有断言更危险 —— 它们**看起来像覆盖**。本项目清出 3 处：

```python
assert job["info"]["size_bytes"] == r.request.stream if False else True
assert "1\n" in body or True
lambda job_id: ... if False else _start_directly(job_id)
```

前两处断言从来没执行过，第三处前一个分支永不可达。
**肉眼扫很容易滑过去**，只有 `ruff` 的 SIM222 和 vulture 的 100% 置信度
能抓到 —— 所以第 2、3 步不能省。

---

## 七、本次清理结果

`e13803b`，−86 / +28 行。

| 类别 | 删除项 |
|---|---|
| 零引用 | `discovery._roots_of`、`launcher.find_running_instance`、`pipeline.fmt_ts`、`models.AcquireReport.all_ok`、`runtime.STATE_STOPPING` |
| 只写不读 | `downloads.Task.started_at` / `finished_at` |
| 被取代 | `downloader.adopt_all_from` |
| 死接口 | `/api/models/status` + `ModelStore.progress()` |
| 恒真/不可达 | `tests/test_server_jobs.py` ×2、`tests/test_pipeline.py` ×1 |
| 未使用测试辅助 | `tests/test_pipeline.py::_seg`、`tests/test_proxy.py::HF_URL` |

删掉死接口后，`ModelStore` 正好剩三个入口点：
`inspect()` / `acquire()` / `download()`。

**仍未处理**：`tools/verify_proxy.py` 全仓零引用。它是个能跑的手工诊断
脚本，但没有任何文档指向它 —— 要么在 README 的代理一节提一句让它可被
发现，要么删掉。
