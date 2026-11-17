#!/usr/bin/env python3
"""
Orbbec Gemini 2 深度相机彩色快照抓取工具 (Orbbec Gemini 2 Snapshot Tool)
========================================================================

【模块功能与应用场景】
用于在 rebotarm 环境中，通过 pyorbbecsdk 驱动对奥比中光 Gemini 2 双目结构光相机
进行快速硬件连接测试与单帧高质量 RGB 彩色快照拍摄，并保存为本地 JPG 文件供后续处理使用。

【运行机制】
1. 实例化 OrbbecGemini2 驱动（默认分辨率 640x480, 30fps）；
2. 循环轮询最多 20 次（约 1 秒），等待首帧稳定曝光的彩色图像到达；
3. 将采集到的有效彩色图像持久化保存到 outputs/camera/orbbec.jpg 并向标准输出打印 JSON 结果；
4. 退出时安全关闭相机句柄。
"""
import json
import os
import sys
import time

# 动态配置 Jetson/主机端驱动搜索路径
sys.path.insert(0, "/home/legion/rebot_grasp-jetson")

# 快照图片落盘路径
OUT = "/home/legion/rebot_web/outputs/camera/orbbec.jpg"


def main() -> None:
    """CLI 入口函数：初始化相机、等待取帧并保存图片。"""
    try:
        import cv2
        from drivers.camera.orbbec_gemini2 import OrbbecGemini2
    except Exception as exc:  # noqa: BLE001
        print(json.dumps({"ok": False, "error": f"导入 Orbbec 驱动失败：{exc}"}))
        return

    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    cam = OrbbecGemini2(width=640, height=480, fps=30)
    try:
        # 步骤 1：打开相机设备并启动数据流
        cam.open()
        color = None
        # 步骤 2：最多尝试 20 轮（每次间隔 50ms）等待图像帧生成
        for _ in range(20):
            color, _depth = cam.get_frame()
            if color is not None:
                break
            time.sleep(0.05)

        if color is None:
            print(json.dumps({"ok": False, "error": "相机已打开但 1 秒内没有彩色帧（USB 带宽/曝光）"}))
            return

        # 步骤 3：将抓取到的彩色帧写入磁盘
        cv2.imwrite(OUT, color)
        print(json.dumps({"ok": True, "path": OUT, "shape": list(color.shape)}))
    except Exception as exc:  # noqa: BLE001
        print(json.dumps({"ok": False, "error": str(exc)[:300]}))
    finally:
        # 步骤 4：释放相机硬件连接
        try:
            cam.close()
        except Exception:
            pass


if __name__ == "__main__":
    main()
