"""
录制进程标准输出终端解析与平滑倒计时状态机 (Recording Output Progress State Machine)
========================================================================================

【模块背景与定位】
在 Web 前端界面中，操作员需要清晰了解当前数据集录制的实时动态：
- 当前第几集、共需录制几集；
- 当前处于什么阶段（初始化 init、正式录制 recording、环境复位 reset、重试等待 preparing_next、
  保存写盘 saving、正常收尾 closing）；
- 当前阶段已运行了多少秒、剩余多少秒倒计时。

【核心设计与状态解析】
1. 终端 ANSI 颜色转义字符过滤：消除终端彩色打印干扰。
2. 状态机迁移解析：
   - 匹配 `Recording episode N` 进入 `recording` 阶段并重置单调计时器；
   - 匹配 `Reset the environment` 进入 `reset` 环境重置阶段；
   - 匹配 `Re-record episode` 进入 `preparing_next` 重录准备阶段；
   - 匹配 `[剧集保存]` 进入 `saving` 视频编码与写盘阶段；
   - 匹配 `Stop recording` 或错误日志进入 `closing` 阶段。
3. 单调时钟平滑计算：利用 `time.monotonic` 提供毫秒级精确的已耗时 `elapsed_s` 与剩余倒计时 `remaining_s`。
"""
import math
import re
import time

# 终端 ANSI 转义序列正则匹配
ANSI = re.compile(r'\x1b\[[0-?]*[ -/]*[@-~]')
# 剧集开始行正则匹配
EPISODE = re.compile(r'(?:^|\s)Recording episode\s+(\d+)\s*$', re.I)


class RecordingProgress:
    """
    录制器输出流状态机解析类。
    持续消费录制子进程的 stdout 文本行，维持当前阶段与时间快照。
    """

    def __init__(self, settings, *, clock=time.monotonic):
        """
        初始化解析状态机。

        Args:
            settings (dict): 数据集录制配置（包含 repo_id, num_episodes, episode_time_s 等）。
            clock (callable, optional): 单调时间戳获取函数。
        """
        self.clock = clock
        self.repo_id = str(settings.get('repo_id') or '')
        self.total = max(1, int(self._number(settings.get('num_episodes'), 1)))
        self.episode_s = self._number(settings.get('episode_time_s'), 30)
        self.reset_s = self._number(settings.get('reset_time_s'), 0)
        self.phase = 'init'
        self.sequence = 0
        self.started = clock()
        self.first_episode = self.raw_episode = None
        self.rerecord = False
        self.startup_error = ''

    @staticmethod
    def _number(value, default):
        """将输入解析为有限非负浮点数，否则返回默认值。"""
        try:
            number = float(value)
            return number if math.isfinite(number) and number >= 0 else default
        except (TypeError, ValueError):
            return default

    @classmethod
    def from_command(cls, command):
        """从命令行参数列表提取 `--dataset.*` 参数并构建状态机实例。"""
        settings = {}
        for argument in command:
            if argument.startswith('--dataset.') and '=' in argument:
                key, value = argument.split('=', 1)
                settings[key.removeprefix('--dataset.')] = value
        return cls(settings)

    def _phase(self, phase):
        """切换当前状态机阶段，若发生变化则递增序列号并刷新阶段开始时间。"""
        if self.phase == phase:
            return False
        self.phase = phase
        self.sequence += 1
        self.started = self.clock()
        return True

    def feed(self, line):
        """
        向状态机喂入一行终端日志文本，解析阶段变化。

        Args:
            line (str): 终端输出行。

        Returns:
            bool: True 表示触发了状态或阶段变化，False 表示无需刷新。
        """
        line = ANSI.sub('', str(line)).strip()
        # 忽略心跳回声，避免误判
        if line.startswith('[进度]'):
            return False
        # 探测启动阶段的主臂连接致命故障
        if self.raw_episode is None and re.search(r'主臂连接失败|主臂舵机检测失败|Servo not found', line, re.I):
            changed = self.startup_error != line
            self.startup_error = line
            return self._phase('closing') or changed
        if self.phase == 'closing':
            return False

        # 匹配录制开始事件
        match = EPISODE.search(line)
        if match:
            episode = int(match[1])
            if self.raw_episode is not None and (
                episode < self.raw_episode or (episode == self.raw_episode and not self.rerecord)
            ):
                return False
            if self.first_episode is None:
                self.first_episode = episode
            self.raw_episode = episode
            self.rerecord = False
            self.phase = 'init'
            return self._phase('recording')

        # 匹配环境复位事件
        if re.search(r'(?:^|\s)Reset the environment\s*$', line, re.I):
            return self._phase('reset') if self.phase == 'recording' else False

        # 匹配重录当前剧集事件
        if re.search(r'(?:^|\s)Re-record episode\s*$', line, re.I):
            if self.phase not in ('recording', 'reset'):
                return False
            self.rerecord = True
            return self._phase('preparing_next')

        # 匹配保存阶段
        if line.startswith('[剧集保存]') or re.search(r'(?:^|\s)Encoding videos for episode\b', line, re.I):
            return self._phase('saving') if self.raw_episode is not None else False

        # 匹配结束收尾
        if re.search(r'(?:^|\s)(?:Stop recording|Recording stopped)\s*$', line, re.I):
            return self._phase('closing')

        return False

    def snapshot(self):
        """
        获取当前录制进度的全量状态快照字典，供 Web 前端轮询或 WebSocket 实时推送。

        Returns:
            dict: 包含 phase, episode, total, elapsed_s, duration_s, remaining_s 等字段。
        """
        elapsed = max(0.0, self.clock() - self.started)
        duration = self.episode_s if self.phase == 'recording' else self.reset_s if self.phase == 'reset' else None
        return {
            'phase': self.phase,
            'sequence': self.sequence,
            'episode': 1 if self.raw_episode is None else min(self.total, self.raw_episode - self.first_episode + 1),
            'raw_episode': self.raw_episode,
            'total': self.total,
            'repo_id': self.repo_id,
            'has_started': self.raw_episode is not None,
            'startup_error': self.startup_error,
            'episode_time_s': self.episode_s,
            'reset_time_s': self.reset_s,
            'elapsed_s': elapsed,
            'duration_s': duration,
            'remaining_s': max(0.0, duration - elapsed) if duration is not None else None
        }
