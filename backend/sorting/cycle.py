"""一个快递的完整处理：悬停看面单 → 面单朝下则翻面 → 面单朝上后向左推走。

所有设备动作经由注入的 io 完成，本模块不直接接触电机，便于不接硬件做测试。
io 需提供：check() phase(name) scene() pick(scene,previous) plan(scene,target,action)
execute(plan) scan() act_flip(target) verify_after_push(target)
"""
from .geometry import SortingError
from .policy import decide, PUSH_LEFT, RESCAN, FLIP, FAIL
from .pushleft import verify_left


def run_cycle(io, cfg, max_rounds=10, max_push_attempts=2):
    rescans = flips = 0;target = None
    for _ in range(max_rounds):
        io.check();scene = io.scene();target = io.pick(scene, target)
        if target is None:
            raise SortingError('目标丢失，已停止')
        io.phase('INSPECT_HOVER');io.execute(io.plan(scene, target, 'inspect'))
        io.phase('SCAN_LABEL');result = io.scan()
        step = decide(result['status'], rescans, flips, max_flips=int(cfg.get('max_flips', 2)))
        if step == RESCAN:
            rescans += 1;continue
        if step == FLIP:
            io.phase('FLIP_FACE_UP');io.act_flip(target);flips += 1;rescans = 0;continue
        if step == FAIL:
            raise SortingError('面单无法确认朝上（扫描 %s，已翻面 %d 次），已停止' % (result['status'], flips))
        return _push_left(io, cfg, target, result, flips, max_push_attempts)
    raise SortingError('处理轮次已达上限，已停止')


def _push_left(io, cfg, target, result, flips, attempts):
    for attempt in range(attempts):
        io.check();scene = io.scene();current = io.pick(scene, target)
        if current is None:  # 已离开工作区
            return {'status': 'done', 'code': result['codes'][0]['text'], 'flips': flips, 'removed': True}
        io.phase('PUSH_LEFT');io.execute(io.plan(scene, current, 'push_left'))
        after = io.verify_after_push(current)
        check = verify_left(current['xyz_m'], after, cfg.get('left_direction'), float(cfg.get('min_left_push_m', .08)))
        if check['reached']:
            return {'status': 'done', 'code': result['codes'][0]['text'], 'flips': flips, **check}
        target = after
    raise SortingError('多次向左推拢后位移仍不足，已停止')
