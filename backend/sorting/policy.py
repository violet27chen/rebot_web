"""面单朝上的决策：纯函数，不接触任何设备。

扫描结果 → 下一步：
  ok         读到单号，说明面单朝上且可读 → 向左推走
  blurry     画面失焦 → 重新采集
  not_found  清晰却没有码 → 先重新采集一次（排除角度问题），仍无码则判定面单朝下 → 翻面
"""
from .geometry import SortingError

PUSH_LEFT, RESCAN, FLIP, FAIL = 'PUSH_LEFT', 'RESCAN', 'FLIP', 'FAIL'


def decide(status, rescans, flips, max_rescans=2, max_flips=2):
    if status == 'ok':
        return PUSH_LEFT
    if status == 'blurry':
        return RESCAN if rescans < max_rescans else FAIL
    if status == 'not_found':
        if rescans < 1:
            return RESCAN
        return FLIP if flips < max_flips else FAIL
    raise SortingError('未知的单号扫描状态：%r' % (status,))
