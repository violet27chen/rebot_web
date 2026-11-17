#!/usr/bin/env python3
"""
LeRobot 已注册双臂/复合机器人类型真实探测工具 (LeRobot Dual/Robot Types Probe)
================================================================================

【模块背景与定位】
在 LeRobot 录制、遥操作和策略推理中，机械臂与示教器的驱动类注册在 LeRobot 的 Python 环境内。
由于不同的环境（甚至不同版本的 LeRobot）所支持的 robot_type 与 teleop_type 类名存在差异
（例如有的叫 seeed_b601_bimanual，有的叫 bi_b601 等），若前端仅靠静态硬编码或猜想拼凑类型名，
必然导致在执行录制指令时因类名不存在而直接崩溃报错。

【设计原则与探查机制】
1. 真实运行环境探查（Real Environment Reflection）：
   由后端 API 在所选的具体 Conda 环境中调用本脚本，动态 import 当前环境中实际安装的 LeRobot 库。
2. 递归子类与注册表反射探测：
   扫描 RobotConfig、TeleoperatorConfig 等核心基类的所有子类（`__subclasses__()`），
   并检查类或模块上的注册表字典（`_registry`, `_choices`, `get_known_choices`），
   精确提取当前环境中真实已注册的机器人类型标识符。
3. 区分双臂与单臂：
   利用 `is_bi` 关键词（bimanual, bi_, dual, two_arm 等）进行分类，
   stdout 只输出单行精简 JSON，避免格式污染。
"""
from __future__ import annotations

import importlib
import importlib.metadata as md
import json
import sys

# LeRobot 机器人模型配置可能存在的模块路径列表
ROBOT_MODULES = (
    "lerobot.robots.config",
    "lerobot.robots",
    "lerobot.common.robots",
    "lerobot.common.robot_devices.robots.configs",
    "lerobot.configs",
    "lerobot.common.configs",
    "lerobot.common.robot_devices.robots.utils",
)

# 示教器配置可能存在的模块路径列表
TELEOP_MODULES = (
    "lerobot.teleoperators.config",
    "lerobot.teleoperators",
    "lerobot.teleoperators.configs",
    "lerobot.common.teleoperators",
    "lerobot.common.teleoperators.configs",
    "lerobot.teleops",
    "lerobot.common.robot_devices.teleop.configs",
    "lerobot.configs",
    "lerobot.common.configs",
)

# 目标反射的基类名称
BASE_CLASSES = (
    "RobotConfig", "BaseRobotConfig", "TeleoperatorConfig", "BaseTeleoperatorConfig",
    "TeleopConfig", "BaseTeleopConfig", "RobotConfigBase",
)

# 常见的注册表属性名与类型名称属性名
REGISTRY_ATTRS = ("_registry", "registry", "_choices", "_choice_registry", "_registered")
NAME_ATTRS = ("_choice_name", "choice_name", "type", "name")
BI_HINTS = ("bimanual", "bi_", "_bi", "dual", "two_arm", "bilateral", "pair")


def is_bi(name: str) -> bool:
    """判断给定的类型名称是否属于双臂复合机器人或双示教器。"""
    low = str(name).lower()
    return any(h in low for h in BI_HINTS) or "bimanual" in low


def subclasses(cls):
    """递归遍历指定类的所有直接与间接子类集合。"""
    out = set()
    for sub in cls.__subclasses__():
        out.add(sub)
        out.update(subclasses(sub))
    return out


def names_from_registry(obj) -> set[str]:
    """从对象或类中反射提取其注册表中暴露出的类型字符串。"""
    found: set[str] = set()
    choices = getattr(obj, "get_known_choices", None)
    if callable(choices):
        try:
            found.update(k for k in choices() if isinstance(k, str))
        except Exception:
            pass
    for attr in REGISTRY_ATTRS:
        try:
            reg = getattr(obj, attr, None)
        except Exception:  # noqa: BLE001
            continue
        if isinstance(reg, dict):
            for k in reg:
                if isinstance(k, str) and k and not k.startswith("_"):
                    found.add(k)
    for attr in NAME_ATTRS:
        try:
            v = getattr(obj, attr, None)
        except Exception:  # noqa: BLE001
            continue
        if isinstance(v, str) and v and not v.startswith("_") and " " not in v:
            found.add(v)
            break
    return found


def collect(module_paths, classes) -> set[str]:
    """在指定模块路径列表中动态加载模块，并提取目标基类的所有子类注册名称。"""
    found: set[str] = set()
    for path in module_paths:
        try:
            mod = importlib.import_module(path)
        except Exception:  # noqa: BLE001
            continue
        # 模块级 dict 形式的注册表
        for attr in REGISTRY_ATTRS:
            reg = getattr(mod, attr, None)
            if isinstance(reg, dict):
                found.update(k for k in reg if isinstance(k, str) and k)
        for cname in classes:
            base = getattr(mod, cname, None)
            if not isinstance(base, type):
                continue
            for cls in {base} | subclasses(base):
                found.update(names_from_registry(cls))
    return {k for k in found if isinstance(k, str) and k.strip()}


def entry_points_by_group(group: str) -> set[str]:
    """通过 Python entry_points 机制发现第三方安装的插件注册类型。"""
    out: set[str] = set()
    try:
        eps = md.entry_points(group=group)
        for ep in eps:
            out.add(ep.name)
    except Exception:
        pass
    return out


def main() -> None:
    """脚本执行入口：收集所有已注册类型并在 stdout 打印结构化单行 JSON。"""
    robot_names = collect(ROBOT_MODULES, BASE_CLASSES)
    teleop_names = collect(TELEOP_MODULES, BASE_CLASSES)

    # 尝试加载 entry point 插件
    robot_names |= entry_points_by_group("lerobot.robots")
    teleop_names |= entry_points_by_group("lerobot.teleoperators")

    # 分类双臂与单臂
    result = {
        "ok": True,
        "robots": {
            "all": sorted(robot_names),
            "bimanual": sorted(n for n in robot_names if is_bi(n)),
            "single": sorted(n for n in robot_names if not is_bi(n)),
        },
        "teleops": {
            "all": sorted(teleop_names),
            "bimanual": sorted(n for n in teleop_names if is_bi(n)),
            "single": sorted(n for n in teleop_names if not is_bi(n)),
        },
    }
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
