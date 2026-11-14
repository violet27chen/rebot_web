"""Find real character-device owners and kill only those owners on explicit release."""
import glob
import os
import re
import signal
import stat
import time
from pathlib import Path


def device_paths(config):
    paths = set(glob.glob('/dev/ttyUSB*') + glob.glob('/dev/ttyACM*') + glob.glob('/dev/video*'))
    for item in (config.get('leader', {}), config.get('follower', {})):
        if item.get('port'): paths.add(item['port'])
    for side in ('left', 'right'):
        arm = config.get('dual_arm', {}).get(side, {})
        for key in ('leader_port', 'follower_port'):
            if arm.get(key): paths.add(arm[key])
    for camera in config.get('cameras', []):
        source = str(camera.get('index_or_path', ''))
        paths.add('/dev/video'+source if source.isdigit() else source)
    # Legacy depth SDK owns a USB bus node, separate from its RGB V4L node.
    for vendor in Path('/sys/bus/usb/devices').glob('*/idVendor'):
        try:
            if vendor.read_text().strip() == '2bc5':
                bus = int(vendor.with_name('busnum').read_text())
                number = int(vendor.with_name('devnum').read_text())
                paths.add(f'/dev/bus/usb/{bus:03d}/{number:03d}')
        except (OSError, ValueError): pass
    allowed = r'/dev/(?:tty[A-Za-z0-9]+|video\d+|serial/(?:by-id|by-path)/[^/]+|v4l/(?:by-id|by-path)/[^/]+|bus/usb/\d{3}/\d{3})'
    return sorted({os.path.realpath(p) for p in paths if re.fullmatch(allowed, str(p))})


def device_identities(paths):
    result = {}
    for path in paths:
        try:
            info = os.stat(path)
            if stat.S_ISCHR(info.st_mode): result[info.st_rdev] = path
        except OSError: pass
    return result


def owners(paths, proc_root='/proc'):
    identities = device_identities(paths); result = []
    targets = set(paths)
    if not targets: return result
    for entry in Path(proc_root).iterdir():
        if not entry.name.isdigit(): continue
        held = set()
        try:
            for fd in (entry/'fd').iterdir():
                try:
                    info = fd.stat()
                    if stat.S_ISCHR(info.st_mode):
                        if info.st_rdev in identities:held.add(identities[info.st_rdev])
                        # Hot-unplugged device nodes no longer stat(), but old FDs
                        # still name the configured device and must be released.
                        try:
                            name=str(fd.readlink()).removesuffix(' (deleted)')
                            if name in targets:held.add(name)
                        except OSError:pass
                except OSError: pass
            try:
                for mapping in (entry/'maps').read_text().splitlines():
                    parts=mapping.split(None,5)
                    if len(parts)==6:
                        name=parts[5].removesuffix(' (deleted)')
                        if name in targets:held.add(name)
            except OSError:pass
            if held:
                # Start time disambiguates reused PIDs. Never expose full command lines.
                start = (entry/'stat').read_text().rsplit(')', 1)[1].split()[19]
                result.append({'pid':int(entry.name), 'name':(entry/'comm').read_text().strip(),
                               'devices':sorted(held), '_start':start})
        except (OSError, ValueError, IndexError): pass
    return result


def kill_device_owners(paths, timeout=2.0):
    protected = {os.getpid(), 1}; parent = os.getppid()
    while parent > 1 and parent not in protected:
        protected.add(parent)
        try: parent = int(Path(f'/proc/{parent}/stat').read_text().rsplit(')', 1)[1].split()[1])
        except (OSError, ValueError, IndexError): break
    killed, errors = {}, {}; deadline = time.monotonic()+timeout
    while True:
        found = owners(paths)
        if not found: break
        for owner in found:
            pid = owner['pid']
            if pid in protected:
                errors[pid] = '控制台服务仍持有设备，无法终止自身'; continue
            fd = None
            try:
                if hasattr(os, 'pidfd_open'): fd = os.pidfd_open(pid)
                current = Path(f'/proc/{pid}/stat').read_text().rsplit(')', 1)[1].split()[19]
                if current != owner['_start']: continue
                # Check that the selected process still holds one of the target nodes.
                if not any(o['pid']==pid and o['_start']==current for o in owners(owner['devices'])): continue
                if fd is not None: signal.pidfd_send_signal(fd, signal.SIGKILL)
                else: os.kill(pid, signal.SIGKILL)
                killed[pid] = {k:v for k,v in owner.items() if not k.startswith('_')}
            except (ProcessLookupError, FileNotFoundError): pass
            except PermissionError: errors[pid] = '权限不足，未能终止设备占用进程'
            except OSError as exc: errors[pid] = str(exc)
            finally:
                if fd is not None: os.close(fd)
        if time.monotonic() >= deadline: break
        time.sleep(.08)
    remaining = [{k:v for k,v in o.items() if not k.startswith('_')} for o in owners(paths)]
    return {'devices':paths, 'killed':list(killed.values()), 'remaining':remaining,
            'errors':[{'pid':pid,'error':err} for pid,err in errors.items() if any(o['pid']==pid for o in remaining)]}
