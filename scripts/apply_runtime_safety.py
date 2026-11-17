"""Persist log-only progress, recorder cleanup and fresh-feedback fixes. No device I/O."""
import importlib.util
from pathlib import Path
import shutil
import subprocess


def package(name):
    spec = importlib.util.find_spec(name)
    if not spec or not spec.submodule_search_locations:
        raise RuntimeError('所选环境缺少 ' + name)
    return Path(next(iter(spec.submodule_search_locations)))


def main():
    root = Path(__file__).resolve().parents[1]
    lerobot = package('lerobot')
    leader = package('lerobot_teleoperator_rebot_arm_102')
    follower = package('lerobot_robot_seeed_b601')
    if 'from .connection_safety import verify_servos, initial_positions' not in (leader/'rebot_arm_102_leader.py').read_text():
        raise RuntimeError('请先运行 scripts/apply_leader_startup.py')
    leader_text = (leader/'rebot_arm_102_leader.py').read_text()
    record_text = (lerobot/'scripts/lerobot_record.py').read_text()
    changes = [
        (lerobot, 'utils/utils.py', 'without launching any speech process', 'no_speech.patch'),
        (lerobot, 'datasets/video_utils.py', 'finalize() only closes parquet writers', 'video_cleanup.patch'),
        (lerobot, 'scripts/lerobot_record.py', 'with finish_cleanup():',
         'record_signal_cleanup.patch' if '[采集收尾] 数据集关闭完成' in record_text else 'record_cleanup.patch'),
        (lerobot, 'scripts/lerobot_teleoperate.py', 'with finish_cleanup():', 'teleop_signal_cleanup.patch'),
        (leader.parent, 'lerobot_teleoperator_rebot_arm_102/rebot_arm_102_leader.py',
         'return read_fresh_feedback(self.bus, self.config.joint_ids, self.config.port,',
         'leader_feedback_retry.patch' if 'return fresh_positions(result, self.config.joint_ids)' in leader_text else 'leader_fresh_feedback.patch'),
        (follower.parent, 'lerobot_robot_seeed_b601/seeed_b601_follower.py',
         '从臂反馈缺失或无效，未使用默认零值', 'follower_feedback.patch'),
    ]
    pending = []
    for directory, relative, marker, patch in changes:
        if marker in (directory/relative).read_text():
            continue
        command = ['patch', '--batch', '--forward', '-p1', '-i', str(root/'vendor_patches'/patch)]
        subprocess.run(command + ['--dry-run'], cwd=directory, check=True)
        pending.append((directory, command))
    shutil.copy2(root/'backend/leader_connection.py', leader/'connection_safety.py')
    shutil.copy2(root/'backend/shutdown_signals.py', lerobot/'utils/rebot_cleanup.py')
    for directory, command in pending:
        subprocess.run(command, cwd=directory, check=True)
    print('[补丁] 已安装无语音收尾、逐关节角度重读及普通停止信号保护（未连接任何设备）')


if __name__ == '__main__':
    main()
