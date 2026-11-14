"""持续分拣：反复处理桌面上的快递，直到桌面清空。

单个包裹：悬停看面单 → 面单朝下则翻面 → 面单朝上后向左推走。
会话：每处理完一个就重新检测，选下一个，直到连续两次检测都没有快递。

所有设备动作经注入的 io 完成，本模块不直接接触电机，便于不接硬件做测试。
io 需提供：check() phase(name) scene() pick(scene,previous) plan(scene,target,action)
execute(plan) scan() act_flip(target) verify_after_push(target) retreat()
"""
import numpy as np
from .geometry import SortingError, PackageError
from .policy import decide, PUSH_LEFT, RESCAN, FLIP, FAIL
from .pushleft import verify_left, left_direction


def run_cycle(io, cfg, target, max_rounds=10, max_push_attempts=2):
    """处理一个包裹。可恢复的单包裹失败抛 PackageError。"""
    rescans = flips = 0
    for _ in range(max_rounds):
        io.check();scene = io.scene();target = io.pick(scene, target)
        if target is None:
            raise PackageError('目标丢失（可能被推出或被遮挡）')
        io.phase('INSPECT_HOVER');io.execute(io.plan(scene, target, 'inspect'))
        io.phase('SCAN_LABEL');result = io.scan()
        step = decide(result['status'], rescans, flips, max_flips=int(cfg.get('max_flips', 2)))
        if step == RESCAN:
            rescans += 1;continue
        if step == FLIP:
            io.phase('FLIP_FACE_UP');io.act_flip(target);flips += 1;rescans = 0;continue
        if step == FAIL:
            if not cfg.get('push_unreadable', True):
                raise PackageError('面单无法确认朝上（扫描 %s，已翻面 %d 次）' % (result['status'], flips))
            # 仍要清空桌面：推走，但明确标记为未识别，由后续人工处理
            out = _push_left(io, cfg, target, None, flips, max_push_attempts)
            out['status'] = 'unreadable';return out
        return _push_left(io, cfg, target, result['codes'][0]['text'], flips, max_push_attempts)
    raise PackageError('处理轮次已达上限')


def _push_left(io, cfg, target, code, flips, attempts):
    for _ in range(attempts):
        io.check();scene = io.scene();current = io.pick(scene, target)
        if current is None:  # 已离开工作区
            return {'status': 'done', 'code': code, 'flips': flips, 'removed': True}
        io.phase('PUSH_LEFT');io.execute(io.plan(scene, current, 'push_left'))
        after = io.verify_after_push(current)
        check = verify_left(current['xyz_m'], after, cfg.get('left_direction'), float(cfg.get('min_left_push_m', .08)))
        if check['reached']:
            return {'status': 'done', 'code': code, 'flips': flips, **check}
        target = after
    raise PackageError('多次向左推拢后位移仍不足')


def _near(a, b, d=.05):
    return a['name'] == b['name'] and np.linalg.norm(np.asarray(a['xyz_m']) - np.asarray(b['xyz_m'])) < d


def run_session(io, cfg, max_packages=50):
    """持续处理直到桌面没有快递。返回统计；无法清空时给出明确原因。"""
    left = left_direction(cfg.get('left_direction'))
    report = {'done': 0, 'unreadable': 0, 'failed': [], 'stopped_because': None}
    blocked = [];empty = unlocatable = 0;handled = 0
    while handled < max_packages:
        io.check()
        if cfg.get('retreat_to_start'):io.retreat()
        scene = io.scene();targets = scene['targets']
        if not targets:
            empty += 1
            if empty >= 2:report['stopped_because'] = 'cleared';return report
            continue
        empty = 0
        usable = [t for t in targets if t.get('valid') and not any(_near(t, b) for b in blocked)]
        if not usable:
            if all(t.get('valid') and any(_near(t, b) for b in blocked) for t in targets):
                report['stopped_because'] = 'only_failed_remain';return report
            unlocatable += 1  # 剩余包裹暂时没有有效深度（遮挡/空洞），重拍几次
            if unlocatable >= 3:report['stopped_because'] = 'unlocatable';return report
            continue
        unlocatable = 0
        # 越靠左越先推，避免后推的包裹被已在左侧的包裹挡住去路
        target = sorted(usable, key=lambda t: -float(np.asarray(t['xyz_m']) @ left))[0]
        handled += 1
        try:
            result = run_cycle(io, cfg, target)
        except PackageError as exc:
            blocked.append(target);report['failed'].append({'target': target['name'], 'xyz_m': target['xyz_m'], 'reason': str(exc)})
            continue
        report['unreadable' if result['status'] == 'unreadable' else 'done'] += 1
    report['stopped_because'] = 'max_packages';return report
