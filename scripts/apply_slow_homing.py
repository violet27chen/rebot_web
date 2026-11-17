"""Idempotent, compatibility-checked patch of local drivers. No device I/O."""
import importlib.util
from pathlib import Path
import shutil
import subprocess


def main():
    root=Path(__file__).resolve().parents[1]
    if not shutil.which('patch'):raise RuntimeError('需要系统 patch 工具：sudo apt install patch')
    spec=importlib.util.find_spec('lerobot_robot_seeed_b601')
    if spec is None or not spec.submodule_search_locations:raise RuntimeError('所选环境缺少 B601 插件')
    pkg=Path(next(iter(spec.submodule_search_locations)))
    # The slow-return diff is based on the bounded mode-readback fix.
    def apply_patch(cwd,name):
        command=['patch','--batch','--forward','-p1','-i',str(root/'vendor_patches'/name)]
        subprocess.run(command+['--dry-run'],cwd=cwd,check=True)
        subprocess.run(command,cwd=cwd,check=True)
    if 'from .mode_safety import ensure_mode_checked' not in (pkg/'seeed_b601_follower.py').read_text():
        apply_patch(pkg.parent,'b601_mode_readback.patch')
    bi=pkg/'config_bi_seeed_b601_dm_follower.py'
    if bi.is_file() and 'force_pos_torque_ration' not in bi.read_text():apply_patch(pkg,'bi_gripper_limits.patch')
    checks=[(pkg.parent,root/'vendor_patches/slow_homing_follower.patch',pkg/'seeed_b601_follower.py','Slow measured-pose return'),
            (root,root/'vendor_patches/slow_homing_sdk.patch',root/'rebot_grasp/sdk/reBotArm_control_py/reBotArm_control_py/controllers/rebotarm_endpose_controller.py','A timeout/obstruction holds the measured pose')]
    for cwd,patch,target,marker in checks:
        if target.is_file() and marker in target.read_text():
            print('[补丁] 已安装慢速收工：'+str(target),flush=True);continue
        if not target.is_file():raise RuntimeError('缺少官方 SDK / 插件源码：'+str(target))
        command=['patch','--batch','--forward','-p1','-i',str(patch)]
        # Unsupported upstream changes are surfaced; never blindly rewrite drivers.
        subprocess.run(command+['--dry-run'],cwd=cwd,check=True)
        subprocess.run(command,cwd=cwd,check=True)
        print('[补丁] 慢速归零更新完成：'+str(target),flush=True)
    # Keep standalone lerobot-teleoperate/record cleanup consistent with Web.
    paired=pkg/'bi_seeed_b601_dm_follower.py'
    if not paired.is_file():raise RuntimeError('缺少双臂 B601 插件源码')
    if 'Coordinated parallel shutdown' not in paired.read_text():
        apply_patch(pkg.parent,'parallel_shutdown.patch')
    elif '_coordinated_shutdown' not in (pkg/'seeed_b601_follower.py').read_text():
        raise RuntimeError('双臂补丁不完整，请检查从臂驱动的同步收工检查')
    shutil.copy2(root/'backend/coordinated_shutdown.py',pkg/'coordinated_shutdown.py')
    # Refresh shared limits even when the original slow-homing patch is present.
    # Otherwise existing installations silently keep the old slower profile.
    for target in (pkg/'homing_profile.py',
            root/'rebot_grasp/sdk/reBotArm_control_py/reBotArm_control_py/controllers/homing_profile.py'):
        shutil.copy2(root/'backend/homing_profile.py',target)
    sdk=root/'rebot_grasp/sdk/reBotArm_control_py/reBotArm_control_py/controllers/rebotarm_endpose_controller.py'
    text=sdk.read_text()
    text=text.replace('def safe_home(self,max_vel=.15,','def safe_home(self,max_vel=.20,')
    text=text.replace('max_vel=min(max_vel,.15)','max_vel=min(max_vel,.20)')
    if text!=sdk.read_text():sdk.write_text(text)
    print('[补丁] 双臂并行收工及阶段同步已安装',flush=True)


if __name__=='__main__':main()
