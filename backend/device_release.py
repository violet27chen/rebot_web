"""
Linux 字符设备占用精准排查与安全终止清理工具 (Hardware Device Release & Kill)
==================================================================================

【模块背景与定位】
在工业控制上位机中，机械臂串口（/dev/ttyUSB*、/dev/ttyACM*）与工业相机（/dev/video*）
属于独占型字符设备。如果前序任务异常崩溃、挂起未释放或外部进程意外占用了设备端口，
新任务启动时会报 "Device or resource busy" 或 "Permission denied" 错误。

【核心安全设计与防护准则】
1. 精准 Linux procfs 反查 (`owners`)：
   - 通过系统调用 `os.stat` 获取目标设备的主次设备号（st_rdev）；
   - 遍历 `/proc/<pid>/fd/*` 与 `/proc/<pid>/maps`，通过对比 `st_rdev` 与软链接目标路径，
     精准定位持有该字符设备文件描述符的真实进程 PID；
   - 支持热插拔断开检测：即使物理设备已被拔出变成 "deleted"，依然能根据旧 FD 识别出残留死锁进程。
2. 进程树安全自保白名单（Process Self-Protection）：
   - 将当前 FastAPI 服务主进程 PID 与 PID 1 (systemd/init) 自动加入受保护集合；
   - 向上递归遍历父进程链（PPID），将控制台主程序及其所有父级祖先进程全部加入保护，
     【绝对禁止强杀服务自身或主终端】，防止 Web 平台自身退出。
3. 严格 PID 复用校验与 Linux pidfd 安全信号：
   - 记录进程的启动时间戳（`stat` 的 start_time），并在发送信号前利用 Linux `pidfd_open`
     进行原子校验，坚决防止 Linux 内核 PID 回收复用导致误杀无关系统进程。
"""
import glob
import os
from pathlib import Path
import re
import signal
import stat
import time


def device_paths(config):
    """
    根据系统全局配置与物理设备节点，汇总所有受管的串口与相机设备路径集合。

    Args:
        config (dict): 平台主配置字典。

    Returns:
        list[str]: 规范化后的系统唯一设备真实路径列表（排重并排序）。
    """
    paths = set(glob.glob('/dev/ttyUSB*') + glob.glob('/dev/ttyACM*') + glob.glob('/dev/video*'))
    for item in (config.get('leader', {}), config.get('follower', {})):
        if item.get('port'):
            paths.add(item['port'])
    for side in ('left', 'right'):
        arm = config.get('dual_arm', {}).get(side, {})
        for key in ('leader_port', 'follower_port'):
            if arm.get(key):
                paths.add(arm[key])
    for camera in config.get('cameras', []):
        source = str(camera.get('index_or_path', ''))
        paths.add('/dev/video' + source if source.isdigit() else source)

    # 兼容通过 USB 节点打开的 OpenNI 深度模块
    for vendor in Path('/sys/bus/usb/devices').glob('*/idVendor'):
        try:
            if vendor.read_text().strip() == '2bc5':
                bus = int(vendor.with_name('busnum').read_text())
                number = int(vendor.with_name('devnum').read_text())
                paths.add(f'/dev/bus/usb/{bus:03d}/{number:03d}')
        except (OSError, ValueError):
            pass

    allowed = r'/dev/(?:tty[A-Za-z0-9]+|video\d+|serial/(?:by-id|by-path)/[^/]+|v4l/(?:by-id|by-path)/[^/]+|bus/usb/\d{3}/\d{3})'
    return sorted({os.path.realpath(p) for p in paths if re.fullmatch(allowed, str(p))})


def device_identities(paths):
    """
    获取指定设备路径集合的主次设备号 (st_rdev) 映射字典。

    Args:
        paths (Iterable[str]): 设备绝对路径集合。

    Returns:
        dict[int, str]: 主次设备号到设备路径的映射。
    """
    result = {}
    for path in paths:
        try:
            info = os.stat(path)
            if stat.S_ISCHR(info.st_mode):
                result[info.st_rdev] = path
        except OSError:
            pass
    return result


def owners(paths, proc_root='/proc'):
    """
    遍历 Linux /proc 虚拟文件系统，精准反查持有目标字符设备的所有活动进程。

    Args:
        paths (Iterable[str]): 目标设备路径列表。
        proc_root (str, optional): proc 文件系统根路径，默认 '/proc'。

    Returns:
        list[dict]: 占用进程列表，每个元素包含 pid, name, devices, _start (启动时间戳)。
    """
    identities = device_identities(paths)
    result = []
    targets = set(paths)
    if not targets or not Path(proc_root).is_dir():
        return result

    for entry in Path(proc_root).iterdir():
        if not entry.name.isdigit():
            continue
        held = set()
        try:
            # 步骤 1：遍历进程的打开文件描述符目录 /proc/<pid>/fd/
            for fd in (entry / 'fd').iterdir():
                try:
                    info = fd.stat()
                    if stat.S_ISCHR(info.st_mode):
                        if info.st_rdev in identities:
                            held.add(identities[info.st_rdev])
                        # 检查已被热插拔拔除但仍被文件句柄持有的设备节点
                        try:
                            name = str(fd.readlink()).removesuffix(' (deleted)')
                            if name in targets:
                                held.add(name)
                        except OSError:
                            pass
                except OSError:
                    pass

            # 步骤 2：检查内存映射 /proc/<pid>/maps
            try:
                for mapping in (entry / 'maps').read_text().splitlines():
                    parts = mapping.split(None, 5)
                    if len(parts) == 6:
                        name = parts[5].removesuffix(' (deleted)')
                        if name in targets:
                            held.add(name)
            except OSError:
                pass

            if held:
                # 提取进程启动时间戳用于防止 PID 复用
                start = (entry / 'stat').read_text().rsplit(')', 1)[1].split()[19]
                result.append({
                    'pid': int(entry.name),
                    'name': (entry / 'comm').read_text().strip(),
                    'devices': sorted(held),
                    '_start': start
                })
        except (OSError, ValueError, IndexError):
            pass
    return result


def kill_device_owners(paths, timeout=2.0):
    """
    安全终止持有指定硬件设备的所有外部进程，受保护的主进程及父级进程除外。

    Args:
        paths (list[str]): 目标设备路径列表。
        timeout (float, optional): 最大等待释放超时时间（秒），默认 2.0s。

    Returns:
        dict: 包含 devices, killed (已杀进程), remaining (残留进程), errors (错误信息)。
    """
    # 步骤 1：构建受保护的自身进程与所有祖先进程白名单集合
    protected = {os.getpid(), 1}
    parent = os.getppid()
    while parent > 1 and parent not in protected:
        protected.add(parent)
        try:
            parent = int(Path(f'/proc/{parent}/stat').read_text().rsplit(')', 1)[1].split()[1])
        except (OSError, ValueError, IndexError):
            break

    killed, errors = {}, {}
    deadline = time.monotonic() + timeout

    # 步骤 2：在超时预算内循环检测并发送终止信号
    while True:
        found = owners(paths)
        if not found:
            break
        for owner in found:
            pid = owner['pid']
            if pid in protected:
                errors[pid] = '控制台服务仍持有设备，无法终止自身'
                continue

            fd = None
            try:
                # 使用现代 Linux pidfd 避免信号发送与 PID 复用竞态
                if hasattr(os, 'pidfd_open'):
                    fd = os.pidfd_open(pid)
                current = Path(f'/proc/{pid}/stat').read_text().rsplit(')', 1)[1].split()[19]
                if current != owner['_start']:
                    continue  # PID 已被新进程复用，跳过不杀

                # 再次确认该进程确实持有目标设备
                if not any(o['pid'] == pid and o['_start'] == current for o in owners(owner['devices'])):
                    continue

                # 发送 SIGKILL 强制终止
                if fd is not None:
                    signal.pidfd_send_signal(fd, signal.SIGKILL)
                else:
                    os.kill(pid, signal.SIGKILL)
                killed[pid] = {k: v for k, v in owner.items() if not k.startswith('_')}
            except (ProcessLookupError, FileNotFoundError):
                pass
            except PermissionError:
                errors[pid] = '权限不足，未能终止设备占用进程'
            except OSError as exc:
                errors[pid] = str(exc)
            finally:
                if fd is not None:
                    os.close(fd)

        if time.monotonic() >= deadline:
            break
        time.sleep(0.08)

    remaining = [{k: v for k, v in o.items() if not k.startswith('_')} for o in owners(paths)]
    return {
        'devices': paths,
        'killed': list(killed.values()),
        'remaining': remaining,
        'errors': [
            {'pid': pid, 'error': err}
            for pid, err in errors.items()
            if any(o['pid'] == pid for o in remaining)
        ]
    }
