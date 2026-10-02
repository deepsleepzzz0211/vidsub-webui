#!/usr/bin/env python3
"""llama-server 的测试替身。

要像到能验证真问题，行为对齐 r2t2-test 里实测过的那个二进制：

- 认 `--host/--port/-m/--mmproj/-c/-ngl/-t/--parallel/--load-mode/-fa/--prio/--jinja`
- `GET /health` 返回带 `"ok"` 的 JSON
- 启动过程往 stdout 写若干行（真二进制会打加载进度，日志要能验证落盘）
- **路径含空格就拒绝启动** —— 这是真二进制实测会报 `invalid argument` 的毛病，
  替身要一样拒，否则"我们传了不含空格的路径"这条根本测不出来

用环境变量模拟故障：
  FAKE_FAIL=exit        启动即退出（非零码），模拟缺权重/页锁抢不到
  FAKE_FAIL=hang        永远不就绪也不退出，验证超时上报
  FAKE_FAIL=late:<秒>   延迟这么久才就绪
  FAKE_DELAY_MODEL=<秒> 模拟加载大权重要时间（默认 0.2）
"""
import http.server
import json
import os
import sys
import time


def parse(argv):
    cfg = {"host": "127.0.0.1", "port": 0, "model": "", "mmproj": "",
           "ctx": 0, "extra": []}
    takes_value = {
        "--host": "host", "--port": "port",
        "-m": "model", "--model": "model", "--mmproj": "mmproj", "-c": "ctx",
    }
    flag_or_value = {"-ngl", "-t", "--parallel", "--load-mode", "-fa", "--prio"}
    i = 0
    while i < len(argv):
        a = argv[i]
        if a in takes_value:
            cfg[takes_value[a]] = argv[i + 1] if i + 1 < len(argv) else ""
            i += 2
            continue
        if a in flag_or_value:
            cfg["extra"] += [a, argv[i + 1] if i + 1 < len(argv) else ""]
            i += 2
            continue
        cfg["extra"].append(a)      # --jinja 之类的开关
        i += 1
    cfg["port"] = int(cfg["port"] or 0)
    return cfg


def main() -> int:
    cfg = parse(sys.argv[1:])
    log = lambda m: print(f"[llama-server] {m}", flush=True)

    log(f"build: fake (argv={' '.join(sys.argv[1:])})")
    log(f"loading model from {cfg['model']} (mmproj={cfg['mmproj'] or '-'})")

    # 真二进制对含空格的模型路径会直接报 invalid argument。替身必须一样，
    # 否则"我们传了不含空格的路径"这条根本测不出来。
    for label, p in (("model", cfg["model"]), ("mmproj", cfg["mmproj"])):
        if p and " " in p:
            log(f"error: invalid argument -- {label} path contains a space: {p!r}")
            return 1
    if not cfg["model"] or not os.path.exists(cfg["model"]):
        log(f"error: failed to load model from {cfg['model']!r}")
        return 1

    fail = os.environ.get("FAKE_FAIL", "")
    if fail == "exit":
        log("error: failed to mlock model memory (page lock contention)")
        return 1
    if fail == "hang":
        # 永不监听、也不退出 —— 验证超时上报。必须真的卡住，不能只是慢。
        log("main: waiting for something that never happens")
        while True:
            time.sleep(1)
    if fail.startswith("late:"):
        time.sleep(float(fail.split(":", 1)[1]))

    delay = float(os.environ.get("FAKE_DELAY_MODEL", "0.2"))
    if delay > 0:
        log(f"llama_model_loader: loaded meta data (waiting {delay}s)")
        time.sleep(delay)

    state = {"n": 0}

    class H(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def _send(self, code, body: bytes, ctype="application/json"):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            state["n"] += 1
            if self.path.startswith("/health"):
                self._send(200, json.dumps({"status": "ok"}).encode())
            elif self.path.startswith("/props"):
                self._send(200, json.dumps({
                    "model_path": cfg["model"], "port": cfg["port"],
                    "boot_count": state["n"]}).encode())
            else:
                self._send(404, b'{"error":"not found"}')

        def log_message(self, *a):
            pass

    srv = http.server.ThreadingHTTPServer((cfg["host"], cfg["port"]), H)
    log(f"main: server is listening on http://{cfg['host']}:{srv.server_port}")
    # 把真实端口回显出来，便于测试在端口 0 时拿到实际端口
    print(f"[llama-server] PORT={srv.server_port}", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())