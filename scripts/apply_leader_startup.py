"""Install bounded Leader startup checks and transactional cleanup, no I/O."""
import importlib.util
from pathlib import Path
import shutil
import subprocess


def package(name):
    spec=importlib.util.find_spec(name)
    if not spec or not spec.submodule_search_locations:
        raise RuntimeError('所选环境缺少 '+name)
    return Path(next(iter(spec.submodule_search_locations)))


def main():
    root=Path(__file__).resolve().parents[1]
    leader=package('lerobot_teleoperator_rebot_arm_102')
    record=package('lerobot')/'scripts/lerobot_record.py'
    operations=[]
    if 'from .connection_safety import verify_servos, initial_positions' not in (leader/'rebot_arm_102_leader.py').read_text():
        operations.append((leader.parent,'leader_connection.patch'))
    if 'Validate leaders first: a missing master must not enable followers' not in record.read_text():
        operations.append((record.parent,'record_leader_first.patch'))
    if operations and not shutil.which('patch'):
        raise RuntimeError('需要系统 patch 工具')
    for directory,name in operations:
        subprocess.run(['patch','--batch','--forward','--dry-run','-p1','-i',str(root/'vendor_patches'/name)],cwd=directory,check=True)
    shutil.copy2(root/'backend/leader_connection.py',leader/'connection_safety.py')
    for directory,name in operations:
        subprocess.run(['patch','--batch','--forward','-p1','-i',str(root/'vendor_patches'/name)],cwd=directory,check=True)
    if 'except BaseException' not in (leader/'bi_rebot_arm_102_leader.py').read_text():
        raise RuntimeError('双主臂回滚补丁不完整')
    print('[补丁] 主臂有界检测、初始实测角度、失败清理和主臂优先连接已安装')


if __name__=='__main__':main()
