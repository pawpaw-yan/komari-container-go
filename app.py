#!/usr/bin/env python3
"""Komari PaaS bootstrap (infrlo 等仅支持 Build/Run Command 的容器平台)。

infrlo 容器默认命令为:
  Build Command: pip install -r requirements.txt
  Run Command:   python app.py

本脚本让 komari（Go 预编译单二进制，前端已内嵌）无需改动平台默认命令即可部署：
  - 构建阶段执行 `python app.py --download-only`，提前从 GitHub Releases
    下载当前架构的 komari 二进制（失败不影响构建，运行时会重试）。
  - 运行阶段 `python3 app.py` 下载（如缺失）后启动 `./komari server`。
    若平台注入 PORT 则直接监听该端口；否则 komari 监听内部端口
    25774，并在 80/3000/5000/8000/8080 上做 TCP 转发兜底，
    适配平台网关固定转发到某个端口的场景。

可用环境变量（均在 infrlo 容器设置页配置）:
  PORT                  平台注入的监听端口（默认 25774）
  KOMARI_VERSION        指定版本，如 1.5.0-fix1（默认自动取最新 release）
  KOMARI_DOWNLOAD_URL   直接指定二进制下载地址（可用 ghproxy 等镜像加速）
  KOMARI_LISTEN         监听地址，默认 0.0.0.0:$PORT
  KOMARI_DATABASE       SQLite 文件路径，默认 ./data/komari.db
                        （平台若提供持久化目录，请指向该目录）
  KOMARI_ARGS           附加给 komari server 的参数（空格分隔）
"""

import json
import os
import platform
import stat
import sys
import urllib.request

REPO = "komari-monitor/komari"
BIN = "./komari"
# 查询最新版本失败时的兜底版本（与上游 release 对齐）
FALLBACK_VERSION = "1.5.0-fix1"

ARCH_MAP = {
    "x86_64": "amd64",
    "amd64": "amd64",
    "aarch64": "arm64",
    "arm64": "arm64",
    "i386": "386",
    "i686": "386",
    "loongarch64": "loong64",
    "riscv64": "riscv64",
}


def detect_arch() -> str:
    return ARCH_MAP.get(platform.machine().lower(), "amd64")


def resolve_version() -> str:
    explicit = os.environ.get("KOMARI_VERSION", "").strip()
    if explicit and explicit != "latest":
        return explicit
    try:
        req = urllib.request.Request(
            f"https://api.github.com/repos/{REPO}/releases/latest",
            headers={"User-Agent": "komari-bootstrap"},
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.load(resp).get("tag_name") or FALLBACK_VERSION
    except Exception as exc:  # 网络受限时兜底，保证容器能起来
        print(f"[komari] query latest version failed ({exc}), fallback to {FALLBACK_VERSION}")
        return FALLBACK_VERSION


def download_url() -> str:
    custom = os.environ.get("KOMARI_DOWNLOAD_URL", "").strip()
    if custom:
        return custom
    tag = os.environ.get("KOMARI_VERSION", "").strip()
    if not tag or tag == "latest":
        tag = resolve_version()
    return f"https://github.com/{REPO}/releases/download/{tag}/komari-linux-{detect_arch()}"


def download(force: bool = False) -> None:
    if os.path.exists(BIN) and not force:
        print(f"[komari] {BIN} already exists, skip download")
        return
    url = download_url()
    print(f"[komari] downloading {url} ...")
    req = urllib.request.Request(url, headers={"User-Agent": "komari-bootstrap"})
    with urllib.request.urlopen(req, timeout=300) as resp:
        data = resp.read()
    if len(data) < 1024 * 1024:
        raise RuntimeError(f"downloaded file too small ({len(data)} bytes), likely a bad url")
    with open(BIN, "wb") as f:
        f.write(data)
    os.chmod(BIN, os.stat(BIN).st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    print(f"[komari] saved {BIN} ({len(data)} bytes)")


INTERNAL_PORT = 25774
# 平台网关可能转发的常见入口端口；绑定失败的端口自动跳过
EXTRA_PORTS = (80, 3000, 5000, 8000, 8080, 30033)


def build_server_cmd(listen: str) -> list:
    cmd = [BIN, "server", "--listen", listen]
    database = os.environ.get("KOMARI_DATABASE", "").strip()
    if database:
        cmd += ["--database", database]
    extra = os.environ.get("KOMARI_ARGS", "").strip()
    if extra:
        cmd += extra.split()
    return cmd


def start_port_forwarders(internal_port: int, ports) -> None:
    """在额外入口端口上起 TCP 转发，全部指向容器内的 komari 端口。"""
    import socket
    import threading

    def pipe(src, dst):
        try:
            while True:
                data = src.recv(65536)
                if not data:
                    break
                dst.sendall(data)
        except OSError:
            pass
        finally:
            try:
                src.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                dst.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

    def forward(listen_port: int) -> None:
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            srv.bind(("0.0.0.0", listen_port))
        except OSError as exc:
            print(f"[komari] forwarder skip port {listen_port}: {exc}")
            return
        srv.listen(128)
        print(f"[komari] forwarding 0.0.0.0:{listen_port} -> 127.0.0.1:{internal_port}")
        while True:
            try:
                client, _ = srv.accept()
            except OSError:
                return
            try:
                upstream = socket.create_connection(("127.0.0.1", internal_port), timeout=10)
            except OSError:
                client.close()
                continue
            threading.Thread(target=pipe, args=(client, upstream), daemon=True).start()
            threading.Thread(target=pipe, args=(upstream, client), daemon=True).start()

    for port in ports:
        threading.Thread(target=forward, args=(port,), daemon=True).start()


def main() -> None:
    download_only = "--download-only" in sys.argv[1:]
    try:
        download(force=download_only)
    except Exception as exc:
        # 构建阶段失败可以容忍（运行时会重试）；运行阶段必须拿到二进制
        if download_only:
            print(f"[komari] build-stage download failed: {exc}")
            return
        raise

    if download_only:
        return

    import signal
    import subprocess

    os.environ.setdefault("GIN_MODE", "release")

    port_env = os.environ.get("PORT", "").strip()
    listen = os.environ.get("KOMARI_LISTEN") or f"0.0.0.0:{port_env or INTERNAL_PORT}"
    cmd = build_server_cmd(listen)
    print(f"[komari] starting: {' '.join(cmd)}")

    proc = subprocess.Popen(cmd)

    # 平台显式注入 PORT / KOMARI_LISTEN 时，komari 已直接监听目标端口，
    # 无需额外转发；否则在常见入口端口上做 TCP 转发兜底，适配网关固定转发端口。
    if not port_env and not os.environ.get("KOMARI_LISTEN"):
        start_port_forwarders(INTERNAL_PORT, EXTRA_PORTS)

    def shutdown(signum, frame):
        proc.terminate()
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)

    exit_code = proc.wait()
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
