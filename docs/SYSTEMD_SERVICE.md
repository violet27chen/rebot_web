# 平台 systemd 服务

本机使用用户级 `rebot-web.service`，已启用 `default.target` 自动启动和 `loginctl enable-linger legion`。因此平台 API 不依赖打开终端或保留浏览器会话，也可以在用户未登录时随用户管理器于开机启动。

前端文件由 FastAPI 提供，不另启 React/Vite 开发服务器。共享视觉、条码/OCR 等子进程随平台按需启动，处于同一个 systemd cgroup；不独立启动第二个摄像头采集服务。ACT 训练、策略评估和电机使能都不会在开机时自动执行。

```bash
systemctl --user status rebot-web
systemctl --user restart rebot-web
systemctl --user stop rebot-web
systemctl --user start rebot-web
journalctl --user -u rebot-web -f
```

访问 `http://127.0.0.1:8000` 或本机局域网地址的 8000 端口。配置文件保留在原项目中。服务源文件是 `deploy/rebot-web.service`，安装位置为 `~/.config/systemd/user/rebot-web.service`。

停止服务时，`scripts/stop_service.py` 先请求当前任务正常停止并等待清理；已有收工归零任务会等待其完成。随后 systemd 对 API 主进程发送 SIGINT，后台正常释放自己的视觉和识别进程。服务不发送强制释放或额外归零指令，禁止超时后自动 SIGKILL，留有 30 分钟清理窗口。硬件故障仍由控制看门狗和急停机制处理，服务管理不是电机急停。

## 无桌面登录时的摄像头权限

本机摄像头属于 `video` 组。当前桌面会话通过 `uaccess` 给 legion 提供了访问 ACL，但 legion 尚不属于 `video` 组。若要重启后不登录桌面也能访问摄像头，需要管理员执行一次：

```bash
sudo usermod -aG video legion
```

然后重启电脑，使用户 systemd 管理器取得新组。`dialout` 已在 legion 的组列表中。不能用开放所有设备权限或以 root 运行整个 Web 平台来代替这些权限设置。

## 当前相机对应关系

左相机原先配置为 `/dev/video2`，该节点实际是笔记本内置相机，已更正到外接 icSpring 的稳定路径。当前对应关系：

- 左：`/dev/v4l/by-path/pci-0000:80:14.0-usb-0:1.2:1.0-video-index0`（当前 `/dev/video0`）。
- 深度相机的 RGB：`/dev/v4l/by-path/pci-0000:80:14.0-usb-0:8.2:1.0-video-index0`（当前 `/dev/video4`）。
- 右：`/dev/v4l/by-path/pci-0000:80:14.0-usb-0:1.4.3.4.2:1.0-video-index0`（当前 `/dev/video6`）。

本次已用真实设备确认三路同时以 MJPG 1920×1080、30 FPS 出帧。稳定路径依赖 USB 插口；更换插口时应重新确认设备对应关系。摄像头绑定修复不涉及机械臂串口或电机动作。
