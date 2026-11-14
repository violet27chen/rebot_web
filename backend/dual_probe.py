#!/usr/bin/env python3
"""在所选 conda 环境里真实枚举已注册的 LeRobot robot / teleop 类型。

由 /api/dual/types 用该环境的 python 执行，stdout 只输出一行 JSON。
目的是让用户从「这台机器实际装了什么」里选双臂复合类型，而不是由网页猜一个
不存在的类型名再拼出必然失败的命令。
"""
from __future__ import annotations

import importlib
import importlib.metadata as md
import json
import sys

ROBOT_MODULES = (
    "lerobot.robots.config",
    "lerobot.robots",
    "lerobot.common.robots",
    "lerobot.common.robot_devices.robots.configs",
    "lerobot.configs",
    "lerobot.common.configs",
    "lerobot.common.robot_devices.robots.utils",
)
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
BASE_CLASSES = (
    "RobotConfig", "BaseRobotConfig", "TeleoperatorConfig", "BaseTeleoperatorConfig",
    "TeleopConfig", "BaseTeleopConfig", "RobotConfigBase",
)
REGISTRY_ATTRS = ("_registry", "registry", "_choices", "_choice_registry", "_registered")
NAME_ATTRS = ("_choice_name", "choice_name", "type", "name")
BI_HINTS = ("bimanual", "bi_", "_bi", "dual", "two_arm", "bilateral", "pair")


def is_bi(name: str) -> bool:
    low = str(name).lower()
    return any(h in low for h in BI_HINTS) or "bimanual" in low


def subclasses(cls):
    out = set()
    for sub in cls.__subclasses__():
        out.add(sub)
        out.update(subclasses(sub))
    return out


def names_from_registry(obj) -> set[str]:
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
            found.update(names_from_registry(base))
            for sub in subclasses(base):
                found.update(names_from_registry(sub))
        # LeRobot 部分版本把配置类直接放在模块里（RobotConfig 子类）
        for _aname, obj in vars(mod).items():
            if isinstance(obj, type) and obj.__name__.endswith("Config"):
                n = names_from_registry(obj)
                if n:
                    found.update(n)
    return found


def main() -> None:
    out = {"lerobot_version": "", "robots": [], "teleops": [],
           "dual_robots": [], "dual_teleops": [], "groups": [], "error": ""}
    try:
        importlib.import_module("lerobot")
        try:
            out["lerobot_version"] = md.version("lerobot")
        except Exception:  # noqa: BLE001
            out["lerobot_version"] = "unknown"
    except Exception as exc:  # noqa: BLE001
        out["error"] = "当前所选环境没有安装 LeRobot：" + str(exc)
        print(json.dumps(out, ensure_ascii=False))
        return

    # LeRobot 0.4 discovers installed distributions; importing a plugin only
    # registers config classes, without constructing or connecting any device.
    try:
        from lerobot.utils.import_utils import register_third_party_plugins
        register_third_party_plugins()
    except (ImportError, AttributeError):
        pass

    # Older versions expose plugins through entry points.
    try:
        eps = md.entry_points()
        groups = set()
        if hasattr(eps, "select"):
            for ep in eps.select(group="lerobot.robots"):
                out["robots"].append(ep.name)
            for ep in eps.select(group="lerobot.teleoperators"):
                out["teleops"].append(ep.name)
            try:
                groups = {g for g in eps.groups if "lerobot" in g.lower()}
            except Exception:  # noqa: BLE001
                pass
        else:  # Python < 3.10 风格
            for group, entries in eps.items():
                if "lerobot" in group.lower():
                    groups.add(group)
                    for ep in entries:
                        if "teleop" in group.lower():
                            out["teleops"].append(ep.name)
                        elif "robot" in group.lower():
                            out["robots"].append(ep.name)
        out["groups"] = sorted(groups)
    except Exception as exc:  # noqa: BLE001
        out["error"] = "读取插件注册表失败：" + str(exc)

    robots = set(out["robots"]) | collect(ROBOT_MODULES, BASE_CLASSES)
    teleops = set(out["teleops"]) | collect(TELEOP_MODULES, BASE_CLASSES)
    # 类注册表里 robot/teleop 常混在一起，用关键字做一次软分类
    for n in list(robots):
        if "teleop" in n.lower() or "leader" in n.lower():
            robots.discard(n)
            teleops.add(n)
    out["robots"] = sorted(n for n in robots if n and not n.startswith("<"))
    out["teleops"] = sorted(n for n in teleops if n and not n.startswith("<"))
    out["dual_robots"] = sorted(n for n in out["robots"] if is_bi(n))
    out["dual_teleops"] = sorted(n for n in out["teleops"] if is_bi(n))
    if not out["robots"] and not out["teleops"] and not out["error"]:
        out["error"] = "已安装 LeRobot，但没能枚举出类型注册表；可在页面手动填写类型名"
    print(json.dumps(out, ensure_ascii=False))


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:  # noqa: BLE001
        print(json.dumps({"lerobot_version": "", "robots": [], "teleops": [],
                          "dual_robots": [], "dual_teleops": [], "groups": [],
                          "error": "探测脚本异常：" + str(exc)}, ensure_ascii=False))
        sys.exit(0)
