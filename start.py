#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
单入口启动器 —— 前后端一起起，不需要分开启动。

    python start.py                      # 标准启动（仅本机 127.0.0.1 访问）
    python start.py --lan                # 局域网模式：监听 0.0.0.0，并打印可访问的局域网 IP
    python start.py --host 0.0.0.0       # 同上，手动指定监听地址
    python start.py --no-chmod           # 跳过启动时的串口检查 / 授权（不推荐）
    python start.py --no-browser         # 不自动打开浏览器
    start_windows.bat                    # Windows 双击启动（自动 chcp 65001）

启动顺序：
  1) 多源回退补齐 Web 依赖（清华 → 阿里 → 官方，带超时，不会卡住）
  2) 串口准备，按平台区分：
       Ubuntu  : 提示 sudo 密码，执行 chmod 666 /dev/ttyUSB* /dev/ttyACM*；
                 密码只经 stdin 传给 sudo，不回显、不记录、不落盘。
       Windows : 只扫描 COM 端口并打印结果（无需 sudo / chmod），
                 端口由设备驱动提供，在设备管理器确认编号与驱动。
     失败会给出明确的手动命令，但不会阻塞网页服务启动。
  3) 启动 FastAPI，前端多页面由后端同端口提供
  4) 自动打开浏览器

Windows 与 Ubuntu 均可在本机运行平台和硬件控制；Windows 通过设备驱动提供的 COM 端口访问机械臂，无需 sudo / chmod。
"""
from __future__ import annotations

import argparse
import getpass
import glob
import os
import socket
import subprocess
import sys
import threading
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "backend"))

# Web 依赖按 清华 → 阿里 → 官方 自动回退：某些网络下镜像的 simple 索引
# 会返回空页面（pip 报 "from versions: none"）但 HTTP 状态仍是 200，单源会启动失败。
PIP_SOURCES = [
    ("清华源", "https://pypi.tuna.tsinghua.edu.cn/simple", "pypi.tuna.tsinghua.edu.cn"),
    ("阿里云", "https://mirrors.aliyun.com/pypi/simple", "mirrors.aliyun.com"),
    ("PyPI 官方", "https://pypi.org/simple", "pypi.org"),
]
PIP_INDEX, PIP_HOST = PIP_SOURCES[0][1], PIP_SOURCES[0][2]

LEADER_GLOB = "/dev/ttyUSB*"      # 幻尔主臂（Ubuntu）
FOLLOWER_GLOB = "/dev/ttyACM*"    # B601-DM 串口桥（Ubuntu）
IS_WINDOWS = os.name == "nt"

# websockets 不能少：uvicorn 缺它时 /ws/logs 握手会 404，训练 Loss 与终端日志无法推送
# multipart 不能少：FastAPI 的 UploadFile / File 路由缺它会在导入后端时直接失败
WEB_MODULES = (("fastapi", "fastapi"), ("uvicorn", "uvicorn"),
               ("serial", "pyserial"), ("websockets", "websockets"),
               ("multipart", "python-multipart"))


def ensure_web_deps() -> None:
    """缺什么装什么，多源回退 + 每源超时，避免单一镜像不可用卡住启动。"""
    import importlib

    def absent() -> list[str]:
        importlib.invalidate_caches()
        out = []
        for mod, pkg in WEB_MODULES:
            try:
                __import__(mod)
            except ImportError:
                out.append(pkg)
        return out

    missing = absent()
    if not missing:
        return
    print("[start] 缺少 Web 依赖：" + ", ".join(missing), flush=True)

    failures: list[str] = []
    for label, index, host in PIP_SOURCES:
        print(f"[start] 尝试 {label}：{index}", flush=True)
        cmd = [sys.executable, "-m", "pip", "install", "-i", index,
               "--trusted-host", host, "--timeout", "15", "--retries", "1", *missing]
        if IS_WINDOWS and label == "清华源":
            # Windows 上清华 simple 索引偶发返回空版本列表（HTTP 200），官方源兜底
            cmd[4:4] = ["--extra-index-url", "https://pypi.org/simple",
                        "--trusted-host", "pypi.org"]
        try:
            r = subprocess.run(cmd, timeout=120)
        except subprocess.TimeoutExpired:
            failures.append(f"{label}：安装超时（2 分钟）")
            continue
        except OSError as exc:
            failures.append(f"{label}：无法启动 pip（{exc}）")
            continue
        if r.returncode != 0:
            failures.append(f"{label}：pip 返回 {r.returncode}")
            continue
        still = absent()
        if still:
            failures.append(f"{label}：安装后仍不可用（{', '.join(still)}）")
            continue
        print(f"[start] 依赖就绪（{label}）", flush=True)
        return

    print("[start] 所有 pip 源均不可用，无法补齐 Web 依赖：", flush=True)
    for line in failures:
        print("        - " + line, flush=True)
    print("        请检查网络（若环境注入了失效的代理，先 unset HTTPS_PROXY HTTP_PROXY），"
          "然后手动执行：", flush=True)
    print("        pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple",
          flush=True)
    sys.exit(1)


def lan_addresses() -> list[str]:
    """列出本机可用于局域网访问的 IPv4（过滤回环地址与 169.254 链路本地地址）。"""
    ips: set[str] = set()
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if not ip.startswith("127."):
                ips.add(ip)
    except OSError:
        pass
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(0.5)
        s.connect(("8.8.8.8", 80))
        ips.add(s.getsockname()[0])
        s.close()
    except OSError:
        pass
    return sorted(ip for ip in ips if not ip.startswith(("127.", "169.254.")))


def _serial_devices() -> list[str]:
    return sorted(set(glob.glob(LEADER_GLOB) + glob.glob(FOLLOWER_GLOB)))


def _run_sudo_chmod(devices: list[str], password: str | None) -> tuple[bool, str]:
    """password=None 表示尝试免密 sudo -n；否则用 sudo -S 从 stdin 读密码。"""
    if password is None:
        cmd = ["sudo", "-n", "chmod", "666", *devices]
        stdin_data = None
    else:
        cmd = ["sudo", "-S", "-p", "", "chmod", "666", *devices]
        stdin_data = password + "\n"
    try:
        r = subprocess.run(cmd, input=stdin_data, capture_output=True,
                           text=True, timeout=20)
    except subprocess.TimeoutExpired:
        return False, "sudo 授权超时（20 秒）"
    except FileNotFoundError:
        return False, "系统未安装 sudo"
    except OSError as exc:
        return False, f"无法执行 sudo：{exc}"
    if r.returncode == 0:
        return True, ""
    return False, (r.stderr or "").strip() or f"sudo 返回 {r.returncode}"


def _windows_com_ports() -> list[str]:
    try:
        from serial.tools import list_ports
        return [p.device for p in list_ports.comports()]
    except Exception:  # noqa: BLE001
        return []


def ensure_serial_permissions(force_prompt: bool = False) -> bool:
    """Linux 提示 sudo 密码执行 chmod；Windows 只做 COM 端口自检（无需授权）。"""
    if IS_WINDOWS:
        print("\n" + "=" * 66)
        print("  串口自检 · Windows（COM 端口，无需 sudo / chmod）")
        print("=" * 66, flush=True)
        ports = _windows_com_ports()
        if ports:
            print(f"[start] 检测到串口：{', '.join(ports)}")
            print("[start] Windows 本机硬件控制就绪：使用设备管理器中的 COM 端口，无需 sudo / chmod。\n",
                  flush=True)
        else:
            print("[start] 未检测到 COM 串口。请确认主从臂已通过 USB 接入，并安装 "
                  "USB 转串口驱动（设备管理器 → 端口 COM 和 LPT）。")
            print("        COM 端口出现后，即可在本机连接并控制机械臂。\n", flush=True)
        return True
    if not sys.platform.startswith("linux"):
        print("[start] 当前不是 Linux，跳过串口授权（macOS 使用各自的设备权限机制）",
              flush=True)
        return True

    print("\n" + "=" * 66)
    print("  串口授权 · 必须在启动网页服务前完成，否则无法连接机械臂")
    print("    sudo chmod 666 /dev/ttyUSB*     # leader 臂（幻尔 Star Arm 102-LD）")
    print("    sudo chmod 666 /dev/ttyACM*     # follower 臂（B601-DM 串口桥）")
    print("=" * 66, flush=True)

    devices = _serial_devices()
    if not devices:
        print("[start] 未发现 /dev/ttyUSB* 或 /dev/ttyACM* 设备。")
        print("        请确认主从臂已通过 USB 连接；接入后可在网页「串口与连接」页面输入密码授权。")
        print("        网页服务仍会继续启动。\n", flush=True)
        return False

    print(f"[start] 检测到串口：{', '.join(devices)}")
    already = all(os.access(d, os.R_OK | os.W_OK) for d in devices)
    if already:
        print("[start] 这些串口当前已具备读写权限（可能来自 udev 规则或上次授权）。")

    interactive = sys.stdin.isatty()
    if not interactive:
        print("[start] 检测到非交互终端，无法弹出密码输入。")
        print("        尝试免密 sudo；若失败，请在网页「串口与连接」页面输入密码授权。")

    # 免密场景先试一次，避免把用户卡在密码提示上
    if not force_prompt and (not interactive or already):
        success, err = _run_sudo_chmod(devices, None)
        if success:
            print("[start] 串口授权成功（sudo 免密）：chmod 666 " + " ".join(devices) + "\n",
                  flush=True)
            return True

    if interactive:
        print("[start] 下面需要当前用户的 sudo 密码（输入不回显，仅用于本次 chmod，不会保存）：",
              flush=True)
        for attempt in range(1, 4):
            try:
                password = getpass.getpass(f"  请输入 sudo 密码（第 {attempt}/3 次，直接回车尝试免密）: ")
            except (EOFError, KeyboardInterrupt):
                print("\n[start] 已取消密码输入。", flush=True)
                break
            success, err = _run_sudo_chmod(devices, password)
            if success:
                print("[start] 串口授权成功：chmod 666 " + " ".join(devices) + "\n", flush=True)
                return True
            low = err.lower()
            if "password" in low or "incorrect" in low or "try again" in low:
                print(f"[start] 密码不正确或 sudo 拒绝（{err}），请重试。", flush=True)
                continue
            print(f"[start] 授权失败：{err}", flush=True)
            break
        # 最后一次兜底：若系统其实配置了免密
        success, err = _run_sudo_chmod(devices, None)
        if success:
            print("[start] 串口授权成功（sudo 免密）：chmod 666 " + " ".join(devices) + "\n",
                  flush=True)
            return True

    print("[start] 未能自动完成串口授权。请手动在终端执行：")
    print("        sudo chmod 666 /dev/ttyUSB*")
    print("        sudo chmod 666 /dev/ttyACM*")
    print("        或启动后在网页「串口与连接」页面输入密码重新授权。")
    print("        网页服务仍会继续启动，但连接机械臂前必须完成授权。\n", flush=True)
    return False


def main() -> None:
    ap = argparse.ArgumentParser(description="机械臂一键操控与训练平台")
    ap.add_argument("--host", default="127.0.0.1", help="监听地址，局域网访问用 0.0.0.0")
    ap.add_argument("--lan", action="store_true",
                    help="局域网模式：监听 0.0.0.0 并打印可访问的局域网 IP")
    ap.add_argument("--port", type=int, default=8000, help="端口，默认 8000")
    ap.add_argument("--no-browser", action="store_true", help="不自动打开浏览器")
    ap.add_argument("--no-chmod", action="store_true",
                    help="跳过启动时的串口检查 / sudo 授权（不推荐）")
    ap.add_argument("--force-password", action="store_true", help="总是弹出 sudo 密码提示（仅 Ubuntu）")
    args = ap.parse_args()

    host = "0.0.0.0" if args.lan else args.host
    os.environ["WEB_HOST"] = host
    os.environ["WEB_PORT"] = str(args.port)

    ensure_web_deps()

    try:
        import main as backend  # backend/main.py
    except ImportError as exc:
        print(f"[start] 后端导入失败：{exc}")
        print("        手动命令：pip install -r requirements.txt -i " + PIP_INDEX)
        sys.exit(1)
    except Exception as exc:  # noqa: BLE001
        print(f"[start] 后端初始化失败：{exc}")
        sys.exit(1)

    if not args.no_chmod:
        ensure_serial_permissions(force_prompt=args.force_password)
    else:
        print("[start] 已按 --no-chmod 跳过串口检查；" +
              ("Windows 请在设备管理器确认 COM 编号" if IS_WINDOWS else
               "若串口无权限，请在网页中输入密码授权。"), flush=True)

    local_url = f"http://127.0.0.1:{args.port}"
    perm = backend.serial_permission_report()
    if IS_WINDOWS:
        perm_text = "无需授权 · COM 端口 " + (", ".join(d["device"] for d in perm["devices"])
                                          or "未检测到")
    else:
        perm_text = "已就绪" if perm["all_writable"] else "需 sudo 授权"
    print("=" * 66)
    print("  机械臂一键操控与训练平台  ·  真机模式（无模拟数据）")
    print(f"  主臂      : 幻尔 Star Arm 102-LD  ({backend.LEADER_TYPE})")
    print(f"  从臂      : Seeed B601-DM         ({backend.FOLLOWER_TYPE})")
    dual_on = backend.dual_enabled(backend.load_config())
    print("  双臂模式  : " + ("已开启 · 四路串口请在「设备连接与校准」页连接" if dual_on
                          else "未开启（原单臂流程）· 可在「设备连接与校准」页开启"))
    print(f"  虚拟环境  : {backend.ENV.active_env or '尚未选择（请在启动向导中选择）'}")
    print(f"  串口状态  : {perm_text}")
    if IS_WINDOWS:
        print("  真机控制  : 本机 COM 端口（Windows 无需 sudo / chmod）")
    print(f"  本机访问  : {local_url}")
    if host in ("0.0.0.0", "::"):
        found = lan_addresses()
        if found:
            for ip in found:
                print(f"  局域网    : http://{ip}:{args.port}")
        else:
            print("  局域网    : 未检测到可用的局域网 IPv4，请检查网卡")
        print(f"  防火墙    : Windows 需放行 TCP {args.port} 入站；"
              f"Ubuntu 用 sudo ufw allow {args.port}/tcp")
        print("  安全提示  : 局域网内任何设备都能调用命令与串口接口，仅在可信网络使用")
        print("  注意      : 网页上的硬件命令在【运行本服务的这台机器】上执行，"
              "不是浏览器所在机器")
    print("=" * 66 + "\n", flush=True)

    if not args.no_browser:
        def _open() -> None:
            try:
                webbrowser.open(local_url)
            except Exception:  # noqa: BLE001
                pass
        threading.Timer(1.5, _open).start()

    try:
        import uvicorn
        # 浏览器还开着 /ws/logs 时，uvicorn 默认会无限等待连接关闭，导致重启卡住；
        # 最多等 5 秒再强制退出，保证 systemctl restart / 急停不会挂住。
        uvicorn.run(backend.app, host=host, port=args.port, log_level="info",
                    timeout_graceful_shutdown=5)
    except KeyboardInterrupt:
        print("\n[start] 已停止。")
    except Exception as exc:  # noqa: BLE001
        print(f"[start] 网页服务启动失败：{exc}")
        sys.exit(1)


if __name__ == "__main__":
    main()
