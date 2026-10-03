"""翻译可复现性诊断：同一输入重复翻译，看输出是否稳定

为什么需要这个工具：09 号票要求"同一输入跑两次产出字节级一致的字幕"。
违反它的症状很隐蔽 —— **英文转写层完全一致，只有中文译文层不同**，
所以只看英文或只看条数根本发现不了。

已经定位过的根因是 llama-server 的 **prompt 缓存**（默认开启）：同一进程内
第二个请求复用上一请求的 KV 前缀，批处理路径变了，浮点累加顺序跟着变，
偶尔 argmax 会落在另一个 token 上。注意 temperature 已经是 0.0（贪心），
所以这不是采样随机性 —— 关温度、减线程、固定种子都修不好它。

本工具就是把这套对照实验固定下来，将来换模型/换 llama.cpp 版本时能一键复验。

用法：
    VIDSUB_DATA_DIR=D:\\vidsub python tools/check_determinism.py [--rounds 5]

退出码：0 全部稳定；1 有任一场景不稳定。
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.request

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "src"))

from vidsub import registry, runtime        # noqa: E402

OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))

# 实测会分叉的句子。第一句在缓存开启时能稳定复现出两种译文。
CASES = (
    "Don't you feel one step closer already? "
    "Like it's already becoming part of your identity.",
    "train five times a week and kick my ass if I don't. Okay.",
)

# 对照场景：(名字, 额外参数)。用来把"是什么导致的"和"是什么不是"分开。
SCENARIOS = (
    ("基线（prompt 缓存默认开启）", []),
    ("关掉 prompt 缓存", ["--no-cache-prompt"]),
)


def _health(port: int) -> bool:
    try:
        with OPENER.open(f"http://127.0.0.1:{port}/health", timeout=2) as r:
            return b'"ok"' in r.read()
    except Exception:
        return False


def _start(port: int, extra: list, model_root: str):
    env = dict(os.environ)
    for k in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY"):
        env.pop(k, None)

    mt = registry.get("mt_model")
    argv = ([runtime.find_llama_server(os.path.join(model_root, "..", "bin"))]
            + ["--host", "127.0.0.1", "--port", str(port)]
            + runtime._common_args()
            + ["-m", registry.rel_path(mt), "--jinja", "-c", "1024"]
            + extra)

    log_path = os.path.join(model_root, "..", "logs", "determinism.log")
    os.makedirs(os.path.dirname(log_path), exist_ok=True)
    lf = open(log_path, "wb")
    try:
        proc = subprocess.Popen(
            argv, cwd=model_root, stdout=lf, stderr=lf, env=env,
            creationflags=(subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0))
    finally:
        lf.close()

    for _ in range(240):
        if _health(port):
            return proc
        if proc.poll() is not None:
            raise SystemExit(
                f"服务启动即退出（码 {proc.returncode}）。日志：{log_path}\n"
                f"{runtime.tail(log_path)}")
        time.sleep(0.5)
    raise SystemExit(f"服务启动超时。日志：{log_path}")


def _kill(proc) -> None:
    runtime.kill_tree(proc.pid)


def _translate(port: int, text: str) -> str:
    prompt = ("Translate the following text into Chinese. Note that you should only "
              f"output the translated result without any additional explanation:\n\n{text}")
    payload = {"messages": [{"role": "user", "content": prompt}],
               "temperature": 0.0, "top_p": 0.6, "top_k": 20, "max_tokens": 1024}
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/v1/chat/completions",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"})
    with OPENER.open(req, timeout=300) as r:
        return json.loads(r.read())["choices"][0]["message"]["content"].strip()


def run_scenario(name: str, extra: list, model_root: str, rounds: int) -> bool:
    """跑一个对照场景。返回是否全部稳定。"""
    port = 8097
    proc = _start(port, extra, model_root)
    stable = True
    try:
        print(f"── {name}  ({' '.join(extra) or '默认参数'}) ──")
        for text in CASES:
            outs = [_translate(port, text) for _ in range(rounds)]
            uniq = sorted(set(outs))
            if len(uniq) == 1:
                print(f"   ✅ 稳定（{rounds} 次一致）  src={text[:40]!r}")
            else:
                stable = False
                print(f"   ❌ 不稳定（{len(uniq)} 种）  src={text[:40]!r}")
                for o in uniq:
                    print(f"        {o}")
        print()
    finally:
        _kill(proc)
    return stable


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--rounds", type=int, default=5)
    args = ap.parse_args(argv)

    model_root = runtime.default_model_root()
    if not os.path.isdir(model_root):
        print(f"权重目录不存在：{model_root}\n"
              f"  先设 VIDSUB_DATA_DIR 指向含 models/ 的目录。")
        return 2

    results = {}
    for name, extra in SCENARIOS:
        results[name] = run_scenario(name, extra, model_root, args.rounds)

    # 结论：关掉缓存后必须稳定，否则说明根因判断需要重做
    ok = all(results.values())
    if results.get("关掉 prompt 缓存") is False:
        print("⚠️ 关掉 prompt 缓存仍然不稳定 —— 根因不止缓存一项，需要重新定位。")
    elif results.get("基线（prompt 缓存默认开启）") is False:
        print("结论：prompt 缓存是唯一变量（基线不稳、关掉后稳定）。"
              "服务参数里必须带 --no-cache-prompt。")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
