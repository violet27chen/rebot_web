#!/usr/bin/env python3
"""Orbbec Gemini 2 彩色帧抓取（rebotarm 环境，使用 pyorbbecsdk）。"""
import json
import os
import sys
import time

sys.path.insert(0, "/home/legion/rebot_grasp-jetson")

OUT = "/home/legion/rebot_web/outputs/camera/orbbec.jpg"


def main() -> None:
    try:
        import cv2
        from drivers.camera.orbbec_gemini2 import OrbbecGemini2
    except Exception as exc:  # noqa: BLE001
        print(json.dumps({"ok": False, "error": f"导入 Orbbec 驱动失败：{exc}"}))
        return
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    cam = OrbbecGemini2(width=640, height=480, fps=30)
    try:
        cam.open()
        color = None
        for _ in range(20):
            color, _depth = cam.get_frame()
            if color is not None:
                break
            time.sleep(0.05)
        if color is None:
            print(json.dumps({"ok": False, "error": "相机已打开但 1 秒内没有彩色帧（USB 带宽/曝光）"}))
            return
        cv2.imwrite(OUT, color)
        print(json.dumps({"ok": True, "path": OUT, "shape": list(color.shape)}))
    except Exception as exc:  # noqa: BLE001
        print(json.dumps({"ok": False, "error": str(exc)[:300]}))
    finally:
        try:
            cam.close()
        except Exception:
            pass


if __name__ == "__main__":
    main()
